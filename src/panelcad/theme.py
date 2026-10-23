"""On-screen color tokens, sheet canvas, and saved appearance settings.

Other PRs should not put hex colors in ``gui/``. Import a token instead::

    from panelcad.theme import ui_hex, canvas_hex, screen_conductor
    from panelcad.gui.theme import ui_qcolor, canvas_qcolor  # when you have Qt

    label.setStyleSheet(f"color: {ui_hex('muted')};")
    painter.setPen(QPen(canvas_qcolor("ink"), 0.35))

Add a new name to ``UI_LIGHT`` / ``UI_DARK`` or ``CANVAS`` rather than a literal.
``ui_hex`` / ``canvas_hex`` are the print-independent UI and sheet colors.
``adapt_print_color`` remaps an existing print hex for the on-screen sheet only.
Print, PDF, DXF, and SVG keep the hex strings in ``render/`` and never call it.

Sheet canvas (paper / white / dark) is separate from the window theme
(light / dark / system). Both are stored outside the project YAML.

Conductor colours
-----------------
IEC 60445:2021 clause 6.2.3 requires AC line conductors to be identified by
BLACK, BROWN, or GREY, listed alphabetically, with no L1/L2/L3 assignment
(https://webstore.iec.ch/en/publication/66712 ; sample text clause 6.2.3 at
https://cdn.standards.iteh.ai/samples/104151/245002a9257645fdbc639f87ad465f14/IEC-60445-2021.pdf).
CENELEC HD 308 S2 identifies the live cores of a multicore cable as brown,
black, and grey, again without assigning those cores to L1/L2/L3
(https://www.nexans.be/en/business/Building-Territories/Building/Distributors/Identification-of-conductors-HD308.html).
NEC 210.5(C) requires ungrounded conductors of different nominal voltage
systems to be identified; it does not name the colours. The brown/orange/yellow
and black/red/blue sets are the identification commonly posted for 277/480 V
and 120/208 V
(https://www.ecmweb.com/national-electrical-code/qa/article/20901695/stumped-by-the-code-rules-for-the-identification-of-branch-circuit-conductors).

When a wire's ``color`` field is set, that name is drawn. When it is empty and
the net is L1/L2/L3 (or the matching T/U, T/V, T/W lead), the on-screen pen
uses the project's drawing standard: IEC brown/black/grey, NEMA/ANSI
black/red/blue, or brown/orange/yellow when that NEMA sheet is marked 480 V.
Nothing is written back to the YAML. On a dark sheet, black is drawn light and
grey is drawn a mid grey, because the true inks disappear. White on a light
sheet gets a dark outline. The sRGB values below are drawing inks for those
names, not numbers taken from a standard. Letter codes follow IEC 60757, which
IEC 60445 cites for the colour list (BK, BN, RD, OG, YE, GN, BU, GY, WH).

Contrast
--------
Normal UI text is checked against WCAG 2.2 Success Criterion 1.4.3
(contrast minimum, Level AA, 4.5:1):
https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html
Relative luminance uses the WCAG definition:
https://www.w3.org/WAI/GL/wiki/Relative_luminance
"""

from __future__ import annotations

import re
import weakref
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

THEMES = ("light", "dark", "system")
CANVASES = ("paper", "white", "dark")
# ``auto`` follows the window theme: paper in light, dark in dark.
# ``get_canvas`` resolves it. Painters never see the word auto.
CANVAS_CHOICES = (*CANVASES, "auto")

# WCAG 2.2 SC 1.4.3 — normal text.
MIN_TEXT_CONTRAST = 4.5
# WCAG 2.2 SC 1.4.11 — non-text UI / graphics that must be seen. Conductor
# strokes use this floor; black and grey on a dark sheet are substituted.
MIN_GRAPHIC_CONTRAST = 3.0

UI_LIGHT: dict[str, str] = {
    "window": "#eceae4",
    "panel": "#f7f4ea",
    "field": "#fffdf8",
    "alt_row": "#efeae0",
    "text": "#1a1a1a",
    "muted": "#5c5648",
    "accent": "#0b4ea2",
    "on_accent": "#f7f4ea",
    "border": "#767164",
    "gridline": "#d4d0c4",
    "selection": "#1d4f8a",
    "selection_text": "#f4f7fb",
    "button": "#f7f4ea",
    "button_hover": "#d9e4f2",
    "menu_hover": "#d9e4f2",
    "header": "#e4dfd2",
    "tab": "#e4dfd2",
    "tab_selected": "#f7f4ea",
    "tab_accent": "#a33b32",
    "status": "#1e3a5f",
    "status_text": "#f7f4ea",
    "dock_title": "#1e3a5f",
    "dock_title_text": "#f7f4ea",
    "banner_info_fg": "#0b3d82",
    "banner_info_bg": "#d6e6fa",
    "banner_success_fg": "#0f4d16",
    "banner_success_bg": "#d7efd7",
    "banner_warning_fg": "#6a3400",
    "banner_warning_bg": "#f8e6c4",
    "banner_error_fg": "#7a1212",
    "banner_error_bg": "#f6d0d0",
    "banner_neutral_fg": "#2c2c2c",
    "banner_neutral_bg": "#e6e4de",
    "sev_error": "#7a1212",
    "sev_warning": "#6a3400",
    "sev_info": "#0b3d82",
    "part_ok": "#146c32",
    "part_warn": "#8a4b00",
    "part_link": "#0b4ea2",
    "log_error": "#7a1212",
    "log_human": "#6a3400",
    "log_ai": "#0b3d82",
    # Limiting SCCR rows and a negative thermal margin. Light values match the
    # 0.5.0 dialogs (#f6d6d6 row, #9b1c1c text).
    "row_alert": "#f6d6d6",
    "alert_text": "#9b1c1c",
}

UI_DARK: dict[str, str] = {
    "window": "#1b1e24",
    "panel": "#262a31",
    "field": "#1e2228",
    "alt_row": "#31363e",
    "text": "#f3f1ea",
    "muted": "#c8c4b8",
    "accent": "#9ec1ff",
    "on_accent": "#1b1e24",
    "border": "#7d8696",
    "gridline": "#454b56",
    "selection": "#3870b8",
    "selection_text": "#f4f7fb",
    "button": "#323844",
    "button_hover": "#3d4654",
    "menu_hover": "#3d4654",
    "header": "#323844",
    "tab": "#323844",
    "tab_selected": "#262a31",
    "tab_accent": "#e07a72",
    "status": "#12151a",
    "status_text": "#f3f1ea",
    "dock_title": "#12151a",
    "dock_title_text": "#f3f1ea",
    "banner_info_fg": "#d6e6ff",
    "banner_info_bg": "#1a3050",
    "banner_success_fg": "#d8f3df",
    "banner_success_bg": "#1a3d28",
    "banner_warning_fg": "#f8e4b8",
    "banner_warning_bg": "#3d3014",
    "banner_error_fg": "#f8d4d4",
    "banner_error_bg": "#3f1c1c",
    "banner_neutral_fg": "#e8e6e0",
    "banner_neutral_bg": "#3a3e46",
    "sev_error": "#f8d4d4",
    "sev_warning": "#f8e4b8",
    "sev_info": "#9ec1ff",
    "part_ok": "#8ddea8",
    "part_warn": "#f0c27a",
    "part_link": "#9ec1ff",
    "log_error": "#f8d4d4",
    "log_human": "#f8e4b8",
    "log_ai": "#9ec1ff",
    "row_alert": "#3f1c1c",
    "alert_text": "#f8d4d4",
}

UI = {"light": UI_LIGHT, "dark": UI_DARK}

# Foreground role, background role. Both themes must clear 4.5:1.
UI_TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("text", "window"),
    ("text", "panel"),
    ("text", "field"),
    ("text", "alt_row"),
    ("text", "button"),
    ("text", "header"),
    ("text", "tab"),
    ("text", "tab_selected"),
    ("text", "menu_hover"),
    ("muted", "window"),
    ("muted", "panel"),
    ("accent", "panel"),
    ("status_text", "status"),
    ("dock_title_text", "dock_title"),
    ("selection_text", "selection"),
    ("banner_info_fg", "banner_info_bg"),
    ("banner_success_fg", "banner_success_bg"),
    ("banner_warning_fg", "banner_warning_bg"),
    ("banner_error_fg", "banner_error_bg"),
    ("banner_neutral_fg", "banner_neutral_bg"),
    ("sev_error", "field"),
    ("sev_error", "alt_row"),
    ("sev_warning", "field"),
    ("sev_warning", "alt_row"),
    ("sev_info", "field"),
    ("sev_info", "alt_row"),
    ("part_ok", "field"),
    ("part_warn", "field"),
    ("part_link", "field"),
    ("log_error", "field"),
    ("log_human", "field"),
    ("log_ai", "field"),
    ("text", "row_alert"),
    ("alert_text", "field"),
    ("alert_text", "alt_row"),
    ("alert_text", "row_alert"),
)

# WCAG 2.2 SC 1.4.11 — non-text contrast, 3:1. Borders, scrollbar handles
# (they use the border token), and the selection against the field.
UI_GRAPHIC_PAIRS: tuple[tuple[str, str], ...] = (
    ("border", "field"),
    ("border", "window"),
    ("selection", "field"),
)

_CANVAS_PAPER: dict[str, str] = {
    "paper": "#f7f4ea",
    "ink": "#111111",
    "xref": "#111111",
    "grid": "#d9d3c3",
    "grid_major": "#cfc6b0",
    "wire": "#0b4ea2",
    "pin": "#c0392b",
    "pin_muted": "#8a8678",
    "pin_fill": "#fff4f0",
    "pin_hot": "#ff6b3d",
    "pin_hot_ring": "#c0392b",
    "pin_unlocked": "#ffe0b2",
    "pin_unlocked_hot": "#f39c12",
    "pin_unlocked_ring": "#d35400",
    "highlight": "#e67e22",
    "highlight_edge": "#7a3e0b",
    "layout_fill": "#d6e4f4",
    "layout_tick": "#7a7568",
    "layout_clip": "#4a4e54",
    "io_fill": "#fff8e1",
    "io_header": "#d9e4f2",
    "ladder_fill": "#fffdf8",
    "ladder_header": "#efe6c8",
    "io_rule": "#c5b48a",
    "network_stroke": "#1565c0",
    "network_fill": "#e3f2fd",
    "notes_stroke": "#8a8678",
    "notes_fill": "#fbfaf3",
    "finding_error": "#c0392b",
    "finding_warning": "#e67e22",
    "finding_info": "#2980b9",
    "finding_leader": "#7f8c8d",
    "finding_glyph": "#fffef8",
    "finding_badge": "#fffef8",
    "wire_preview": "#e67e22",
    "review": "#0b4ea2",
    "symbol_box": "#8a8678",
    "attr": "#0b4ea2",
    "style_fill": "#ffffff",
    "strip_terminal": "#f7f4ea",
    "strip_end_plate": "#eeeae0",
    "strip_end_stop": "#d9d3c5",
    "strip_partition": "#c8c2b4",
    "strip_pe": "#e5f0dc",
    "strip_rail": "#c5c8cc",
    "strip_rail_stroke": "#5c636a",
    "strip_ink": "#222222",
    "strip_selected": "#0b4ea2",
    "strip_stop": "#4a4a4a",
    "strip_pe_mark": "#2e5a32",
    "strip_screw": "#333333",
    "strip_jumper": "#c45c26",
    "enclosure_paper": "#f4f1ea",
    "enclosure_wall": "#3d3a34",
    "enclosure_wall_fill": "#fffdf8",
    "enclosure_duct": "#d8c48a",
    "enclosure_duct_edge": "#6a6458",
    "enclosure_rail": "#7a8188",
    "enclosure_device": "#6b8f71",
    "enclosure_block": "#6a7ea0",
    "enclosure_door": "#a06b6b",
    "enclosure_ink": "#1b1b1b",
    "enclosure_label": "#2f2c28",
    "enclosure_other": "#8a8a8a",
    "enclosure_backplate": "#f3f1ea",
    "needs_info": "#c45c00",
    "needs_info_fill": "#ffe3b3",
}

_CANVAS_WHITE = dict(_CANVAS_PAPER)
_CANVAS_WHITE.update(
    {
        "paper": "#ffffff",
        "grid": "#d5d5d5",
        "grid_major": "#c4c4c4",
        "notes_fill": "#f7f7f7",
        "strip_terminal": "#ffffff",
        "enclosure_paper": "#f7f7f7",
        "enclosure_wall_fill": "#ffffff",
        "style_fill": "#ffffff",
    }
)

_CANVAS_DARK: dict[str, str] = {
    "paper": "#2a2e34",
    "ink": "#f3f1ea",
    "xref": "#f3f1ea",
    "grid": "#3e4550",
    "grid_major": "#454c58",
    "wire": "#8eb6ff",
    "pin": "#ff8d7a",
    "pin_muted": "#7a8494",
    "pin_fill": "#5a3028",
    "pin_hot": "#ff6b3d",
    "pin_hot_ring": "#ffb0a4",
    "pin_unlocked": "#6b4a20",
    "pin_unlocked_hot": "#f0a050",
    "pin_unlocked_ring": "#ffb15a",
    "highlight": "#ffb15a",
    "highlight_edge": "#f3f1ea",
    "layout_fill": "#4d6e96",
    "layout_tick": "#a8a297",
    "layout_clip": "#c5c9ce",
    "io_fill": "#3a3828",
    "io_header": "#2a3d52",
    "ladder_fill": "#3a3428",
    "ladder_header": "#4a4032",
    "io_rule": "#8a7a55",
    "network_stroke": "#8eb6ff",
    "network_fill": "#1e3348",
    "notes_stroke": "#b7b1a4",
    "notes_fill": "#343830",
    "finding_error": "#ff6b6b",
    "finding_warning": "#ffb15a",
    "finding_info": "#7eb6ff",
    "finding_leader": "#b7b3aa",
    "finding_glyph": "#1a1a1a",
    "finding_badge": "#f3f1ea",
    "wire_preview": "#ffb15a",
    "review": "#8eb6ff",
    "symbol_box": "#b7b1a4",
    "attr": "#9ec1ff",
    "style_fill": "#3a3e46",
    "strip_terminal": "#3a3e46",
    "strip_end_plate": "#4a463c",
    "strip_end_stop": "#5c564c",
    "strip_partition": "#6a6458",
    "strip_pe": "#2e4634",
    "strip_rail": "#8b9096",
    "strip_rail_stroke": "#d5d8dc",
    "strip_ink": "#f3f1ea",
    "strip_selected": "#8eb6ff",
    "strip_stop": "#d0d0d0",
    "strip_pe_mark": "#8ddea8",
    "strip_screw": "#e8e6e0",
    "strip_jumper": "#ff9a3c",
    "enclosure_paper": "#2a2e34",
    "enclosure_wall": "#d5d0c6",
    "enclosure_wall_fill": "#3a3e46",
    "enclosure_duct": "#5b5446",
    "enclosure_duct_edge": "#d5cbb8",
    "enclosure_rail": "#b7bcc2",
    "enclosure_device": "#8dcea0",
    "enclosure_block": "#9eb0d0",
    "enclosure_door": "#e0a0a0",
    "enclosure_ink": "#1a1a1a",
    "enclosure_label": "#1a1a1a",
    "enclosure_other": "#b7bcc2",
    "enclosure_backplate": "#32363d",
    "needs_info": "#ffb347",
    "needs_info_fill": "#5a3a10",
}

CANVAS = {"paper": _CANVAS_PAPER, "white": _CANVAS_WHITE, "dark": _CANVAS_DARK}

CANVAS_TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("ink", "paper"),
    ("xref", "paper"),
)

# Drawing inks for named conductor colours. Not values from IEC or NEC.
CONDUCTOR_TRUE: dict[str, str] = {
    "black": "#1a1a1a",
    "red": "#c0392b",
    "blue": "#1558c0",
    "brown": "#6b3e1f",
    "orange": "#d35400",
    "yellow": "#8a6a00",
    "grey": "#5e5e5e",
    "green": "#1b7f3a",
    "white": "#f4f4f4",
}

# On-screen substitutes used only when the true ink fails 3:1 on that sheet.
CONDUCTOR_DARK: dict[str, str] = {
    "black": "#f2f2f2",
    "red": "#ff7a6e",
    "blue": "#8eb6ff",
    "brown": "#e0a078",
    "orange": "#ff9a3c",
    "yellow": "#f0d050",
    "grey": "#8b939e",
    "green": "#7dcea0",
    "white": "#f7f7f7",
}

# IEC 60757 letter codes for the colours this app draws.
_COLOR_ALIASES = {
    "black": "black",
    "bk": "black",
    "red": "red",
    "rd": "red",
    "blue": "blue",
    "bu": "blue",
    "brown": "brown",
    "bn": "brown",
    "orange": "orange",
    "og": "orange",
    "yellow": "yellow",
    "ye": "yellow",
    "grey": "grey",
    "gray": "grey",
    "gy": "grey",
    "green": "green",
    "gn": "green",
    "white": "white",
    "wh": "white",
    "green/yellow": "green",
    "green-yellow": "green",
    "gnye": "green",
}

_PHASE_INDEX = {
    "l1": 1,
    "t1": 1,
    "u": 1,
    "u1": 1,
    "l2": 2,
    "t2": 2,
    "v": 2,
    "v1": 2,
    "l3": 3,
    "t3": 3,
    "w": 3,
    "w1": 3,
}

_IEC_PHASE = ("brown", "black", "grey")
_NEMA_PHASE = ("black", "red", "blue")
_NEMA_480_PHASE = ("brown", "orange", "yellow")

_SCREEN_INK: ContextVar[str | None] = ContextVar("panelcad_screen_ink", default=None)

MARGIN_PX = 8
SPACING_PX = 6

# Bound methods are held through ``weakref.WeakMethod`` so a window that is
# never closed is not pinned by the theme. Plain functions are held strongly.
_listeners: list[Callable[[], Callable[[], None] | None]] = []
_notify_depth = 0


def contrast_hex(a: str, b: str) -> float:
    """WCAG contrast ratio of two ``#rrggbb`` colors."""

    lighter = max(_luminance(a), _luminance(b))
    darker = min(_luminance(a), _luminance(b))
    return (lighter + 0.05) / (darker + 0.05)


def ui_palette(theme: str | None = None) -> dict[str, str]:
    name = theme or resolved_theme_name()
    if name not in UI:
        name = "light"
    return UI[name]


def ui_hex(role: str, theme: str | None = None) -> str:
    try:
        return ui_palette(theme)[role]
    except KeyError as exc:
        raise KeyError(f"unknown UI color {role!r}") from exc


def canvas_palette(canvas: str | None = None) -> dict[str, str]:
    name = canvas or get_canvas()
    if name not in CANVAS:
        name = "paper"
    return CANVAS[name]


class CanvasColor(str):
    """A canvas token. The on-screen painter leaves it as drawn.

    Print hexes that happen to equal a token are still adapted. Matching
    tokens by hex value paints the dark backplate cream, because that cream
    is also the dark ink.
    """


def canvas_hex(role: str, canvas: str | None = None) -> str:
    try:
        return CanvasColor(canvas_palette(canvas)[role])
    except KeyError as exc:
        raise KeyError(f"unknown canvas color {role!r}") from exc


def push_screen_ink(hex_color: str | None):
    """While set, on-screen ``draw_world_text`` uses this ink. Print leaves it unset."""

    return _SCREEN_INK.set(hex_color)


def reset_screen_ink(token) -> None:
    _SCREEN_INK.reset(token)


def active_screen_ink() -> str | None:
    return _SCREEN_INK.get()


def normalize_conductor(name: str) -> str:
    token = re.sub(r"[\s_]+", "", str(name or "").strip().lower())
    return _COLOR_ALIASES.get(token, "")


def phase_color_name(
    color: str,
    net: str,
    *,
    standard: str = "",
    voltage: float | None = None,
) -> str:
    """On-screen conductor name. Empty when the wire is not a coloured phase."""

    named = normalize_conductor(color)
    if named:
        return named
    token = re.sub(r"[^a-z0-9]", "", str(net or "").strip().lower())
    index = _PHASE_INDEX.get(token)
    if not index:
        return ""
    if "iec" in str(standard or "").lower():
        return _IEC_PHASE[index - 1]
    if voltage is not None and float(voltage) >= 400.0:
        return _NEMA_480_PHASE[index - 1]
    return _NEMA_PHASE[index - 1]


def screen_conductor(name: str, canvas: str | None = None) -> tuple[str, str | None]:
    """``(core, outline)`` for one named conductor on a sheet canvas.

    ``outline`` is set only when the substitute still fails the 3:1 floor
    (white on a light sheet). Black and grey on a dark sheet use a light and
    a mid grey substitute, with no coloured halo, so they stay apart from
    blue. Export does not call this.
    """

    key = normalize_conductor(name) or name
    if key not in CONDUCTOR_TRUE:
        raise KeyError(f"unknown conductor colour {name!r}")
    mode = canvas or get_canvas()
    if mode not in CANVAS:
        mode = "paper"
    paper = canvas_hex("paper", mode)
    true = CONDUCTOR_TRUE[key]
    core = true
    outline: str | None = None
    if contrast_hex(true, paper) < MIN_GRAPHIC_CONTRAST:
        core = CONDUCTOR_DARK.get(key, true)
        if contrast_hex(core, paper) < MIN_GRAPHIC_CONTRAST:
            outline = canvas_hex("ink", mode)
    return core, outline


def adapt_print_color(color: str, canvas: str | None = None) -> str:
    """Map a print hex to the on-screen sheet. Paper canvas returns it unchanged.

    PDF, DXF, and SVG must not call this. Qt's on-screen painter does.
    """

    raw = str(color or "").strip()
    if not raw:
        return raw
    mode = canvas or get_canvas()
    if mode == "paper" or mode not in CANVAS:
        return raw
    if mode == "white":
        return "#ffffff" if _paperish(raw) else raw
    if _near_black(raw):
        return canvas_hex("ink", "dark")
    if _paperish(raw) or _luminance(raw) > 0.72:
        return _mix("#32363e", raw, 0.16)
    paper = canvas_hex("paper", "dark")
    if contrast_hex(raw, paper) < MIN_GRAPHIC_CONTRAST:
        return _lift(raw, canvas_hex("ink", "dark"), paper, MIN_GRAPHIC_CONTRAST)
    return raw


def resolved_theme(choice: str, *, system_dark: bool | None) -> str:
    """``light`` or ``dark``. Unknown OS scheme falls back to light."""

    key = str(choice or "").strip().lower()
    if key == "system":
        return "dark" if system_dark else "light"
    if key in {"light", "dark"}:
        return key
    raise ValueError(f"theme must be light, dark, or system (got {choice!r})")


def resolved_theme_name() -> str:
    """Active light/dark palette. Without Qt, Match Windows falls back to light."""

    choice = get_theme()
    if choice != "system":
        return resolved_theme(choice, system_dark=False)
    dark = _qt_system_dark()
    return resolved_theme("system", system_dark=dark)


def config_dir() -> Path:
    from panelcad.settings import config_dir as _config_dir

    return _config_dir()


def settings_path() -> Path:
    from panelcad.settings import settings_path as _settings_path

    return _settings_path()


# File > Open Recent and the start page share this cap.
RECENT_LIMIT = 10

_slice: dict | None = None
_slice_gen = -1


def _appearance() -> dict:
    """Theme, canvas, recent paths, and the phase flag. No copy on the hot path."""

    global _slice, _slice_gen
    from panelcad.settings import generation, peek

    gen = generation()
    if _slice is not None and _slice_gen == gen:
        return _slice
    raw = peek()
    theme = str(raw.get("theme") or "").strip().lower()
    canvas = str(raw.get("canvas") or "").strip().lower()
    if theme not in THEMES:
        theme = "light"
    if canvas not in CANVAS_CHOICES:
        canvas = "paper"
    recent = raw.get("recent")
    paths: list[str] = []
    if isinstance(recent, list):
        for item in recent:
            if not isinstance(item, str):
                continue
            if not item.strip():
                continue
            paths.append(item)
            if len(paths) >= RECENT_LIMIT:
                break
    _slice = {
        "theme": theme,
        "canvas": canvas,
        "recent": paths,
        "phase_colors": _phase_flag(raw.get("phase_colors", True)),
    }
    _slice_gen = gen
    return _slice


def load_settings() -> dict:
    """Appearance slice. The file may hold other keys; ``_write_settings`` keeps them."""

    data = _appearance()
    return {
        "theme": data["theme"],
        "canvas": data["canvas"],
        "recent": list(data["recent"]),
        "phase_colors": data["phase_colors"],
    }


def get_theme() -> str:
    return str(_appearance()["theme"])


def get_canvas_choice() -> str:
    """Saved sheet choice, including ``auto``."""

    return str(_appearance()["canvas"])


def get_canvas() -> str:
    """Resolved sheet: paper, white, or dark. ``auto`` follows the window theme."""

    choice = get_canvas_choice()
    if choice == "auto":
        return "dark" if resolved_theme_name() == "dark" else "paper"
    return choice


def set_theme(theme: str) -> dict:
    key = str(theme or "").strip().lower()
    if key not in THEMES:
        raise ValueError(f"theme must be light, dark, or system (got {theme!r})")
    data = load_settings()
    data["theme"] = key
    _write_settings(data)
    _notify()
    return public_settings()


def set_canvas(canvas: str) -> dict:
    key = str(canvas or "").strip().lower()
    if key not in CANVAS_CHOICES:
        raise ValueError(f"canvas must be paper, white, dark, or auto (got {canvas!r})")
    data = load_settings()
    data["canvas"] = key
    _write_settings(data)
    _notify()
    return public_settings()


def phase_colors_enabled() -> bool:
    """On-screen phase colours. Print, PDF, DXF, and SVG never use this."""

    return bool(_appearance()["phase_colors"])


def parse_phase_enabled(value: object) -> bool:
    """``on`` / ``off`` and the usual boolean spellings. Anything else is an error."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "on", "yes"}:
        return True
    if text in {"0", "false", "off", "no"}:
        return False
    raise ValueError("phase must be on or off")


def set_phase_colors(enabled: bool) -> dict:
    data = load_settings()
    data["phase_colors"] = bool(enabled)
    _write_settings(data)
    _notify()
    return public_settings()


def public_settings() -> dict:
    return {
        "theme": get_theme(),
        "canvas": get_canvas_choice(),
        "resolved_theme": resolved_theme_name(),
        "resolved_canvas": get_canvas(),
        "phase_colors": phase_colors_enabled(),
    }


def remember_project(path: str) -> None:
    from panelcad.io.winpath import path_key, stored_path

    text = stored_path(path)
    key = path_key(text)
    if not key:
        return
    data = load_settings()
    recent = [item for item in data["recent"] if path_key(item) != key]
    recent.insert(0, text)
    data["recent"] = recent[:RECENT_LIMIT]
    _write_settings(data)


def forget_project(path: str) -> None:
    from panelcad.io.winpath import path_key

    key = path_key(path)
    if not key:
        return
    data = load_settings()
    data["recent"] = [item for item in data["recent"] if path_key(item) != key]
    _write_settings(data)


def clear_recent_projects() -> None:
    data = load_settings()
    data["recent"] = []
    _write_settings(data)


def recent_projects() -> list[str]:
    return list(_appearance()["recent"])


def _listener_ref(callback: Callable[[], None]) -> Callable[[], Callable[[], None] | None]:
    if getattr(callback, "__self__", None) is not None and hasattr(callback, "__func__"):
        return weakref.WeakMethod(callback)

    def _strong() -> Callable[[], None]:
        return callback

    return _strong


def _owner_deleted(callback: Callable[[], None]) -> bool:
    """True for a bound method of a Qt object whose C++ side is gone."""

    owner = getattr(callback, "__self__", None)
    if owner is None:
        return False
    try:
        import shiboken6
    except ImportError:
        return False
    try:
        return isinstance(owner, shiboken6.Shiboken.Object) and not shiboken6.isValid(owner)
    except Exception:  # noqa: BLE001 — a half-destroyed wrapper is not a listener
        return True


def _live_listeners() -> list[Callable[[], None]]:
    live = []
    for ref in list(_listeners):
        target = ref()
        if target is None or _owner_deleted(target):
            try:
                _listeners.remove(ref)
            except ValueError:
                pass
            continue
        live.append(target)
    return live


def subscribe(callback: Callable[[], None]) -> None:
    if callback not in _live_listeners():
        _listeners.append(_listener_ref(callback))


def unsubscribe(callback: Callable[[], None]) -> None:
    for ref in list(_listeners):
        target = ref()
        if target is None or target == callback:
            try:
                _listeners.remove(ref)
            except ValueError:
                pass


def banner_roles(status: str) -> tuple[str, str]:
    """``(fg_role, bg_role)`` for an AI dock banner."""

    key = str(status or "").strip().lower()
    if key in {"working", "info"}:
        return ("banner_info_fg", "banner_info_bg")
    if key in {"idle", "success"}:
        return ("banner_success_fg", "banner_success_bg")
    if key == "error":
        return ("banner_error_fg", "banner_error_bg")
    if key == "warning":
        return ("banner_warning_fg", "banner_warning_bg")
    return ("banner_neutral_fg", "banner_neutral_bg")


def _write_settings(data: dict) -> None:
    from panelcad.settings import update

    update(
        {
            "theme": data["theme"],
            "canvas": data["canvas"],
            "recent": list(data["recent"]),
            "phase_colors": bool(data.get("phase_colors", True)),
        }
    )


def _phase_flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value)


def refresh_appearance() -> None:
    """Tell open windows the saved theme or canvas changed."""

    _notify()


def _notify() -> None:
    global _notify_depth
    if _notify_depth:
        return
    _notify_depth += 1
    try:
        for callback in _live_listeners():
            callback()
    finally:
        _notify_depth -= 1


def _qt_system_dark() -> bool | None:
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        return None
    app = QApplication.instance()
    if app is None:
        return None
    try:
        from PySide6.QtCore import Qt

        scheme = app.styleHints().colorScheme()
    except Exception:  # noqa: BLE001
        return None
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return None


def _channel(value: float) -> float:
    value = value / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _rgb(color: str) -> tuple[int, int, int]:
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (0, 0, 0)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _luminance(color: str) -> float:
    red, green, blue = _rgb(color)
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def _paperish(color: str) -> bool:
    red, green, blue = _rgb(color)
    chroma = max(red, green, blue) - min(red, green, blue)
    return _luminance(color) > 0.82 and chroma < 28


def _near_black(color: str) -> bool:
    red, green, blue = _rgb(color)
    return max(red, green, blue) <= 50


def _mix(base: str, tint: str, tint_amount: float) -> str:
    br, bg, bb = _rgb(base)
    tr, tg, tb = _rgb(tint)
    amount = max(0.0, min(1.0, tint_amount))

    def mix(a: int, b: int) -> int:
        return int(round(a * (1.0 - amount) + b * amount))

    return f"#{mix(br, tr):02x}{mix(bg, tg):02x}{mix(bb, tb):02x}"


def _lift(color: str, toward: str, paper: str, minimum: float) -> str:
    current = color
    for step in range(1, 13):
        current = _mix(color, toward, step / 12.0)
        if contrast_hex(current, paper) >= minimum:
            return current
    return toward
