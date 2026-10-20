"""Read an SVG as vectors. Paths and lines become strokes. Text stays text.

Coordinates follow the file: transforms, groups, and the viewBox. A length the
file does not state is needs-info. Outlined glyphs are text only when the file
says so (a text element, a font on that object, or an explicit string). The
characters are never inferred from the shape.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, NamedTuple
from xml.etree import ElementTree as ET

from panelcad.importer.vectors import (
    _r,
    _role,
    _span,
    finish_conductor_strokes,
    omit_border_line,
    omit_border_rect,
    stroke_role,
)

PX_MM = 25.4 / 96.0
PT_PER_MM = 72.0 / 25.4
# Cap-height center above an alphabetic baseline, as a fraction of the em (Y down).
_BASELINE = 0.35
_ABS_MM = {"px": PX_MM, "pt": 25.4 / 72.0, "pc": 25.4 / 6.0, "mm": 1.0, "cm": 10.0, "in": 25.4, "q": 0.25}
_NUM = r"[+-]?(?:\d+\.\d+|\d+\.|\.\d+|\d+)(?:[eE][+-]?\d+)?"
_NUM_RE = re.compile(_NUM)
_LENGTH_RE = re.compile(rf"^({_NUM})([A-Za-z%]*)$")
_XLINK = "{http://www.w3.org/1999/xlink}href"
_SKIP = {
    "defs",
    "symbol",
    "style",
    "script",
    "metadata",
    "title",
    "desc",
    "clipPath",
    "mask",
    "pattern",
    "marker",
    "linearGradient",
    "radialGradient",
    "filter",
    "font",
    "font-face",
    "glyph",
    "missing-glyph",
}
_INITIAL = {
    "fill": "black",
    "stroke": "none",
    "stroke-width": "1",
    "fill-opacity": "1",
    "stroke-opacity": "1",
    "opacity": "1",
    "font-size": "16px",
    "font-family": "",
    "font": "",
    "text-anchor": "start",
    "dominant-baseline": "auto",
    "alignment-baseline": "auto",
    "display": "inline",
    "visibility": "visible",
    "color": "black",
}
_NAMED = {
    "black": (0.0, 0.0, 0.0),
    "white": (1.0, 1.0, 1.0),
    "red": (1.0, 0.0, 0.0),
    "green": (0.0, 0.502, 0.0),
    "blue": (0.0, 0.0, 1.0),
    "yellow": (1.0, 1.0, 0.0),
    "orange": (1.0, 0.647, 0.0),
    "gray": (0.502, 0.502, 0.502),
    "grey": (0.502, 0.502, 0.502),
    "cyan": (0.0, 1.0, 1.0),
    "magenta": (1.0, 0.0, 1.0),
    "lime": (0.0, 1.0, 0.0),
    "navy": (0.0, 0.0, 0.502),
    "maroon": (0.502, 0.0, 0.0),
    "purple": (0.502, 0.0, 0.502),
    "silver": (0.753, 0.753, 0.753),
    "teal": (0.0, 0.502, 0.502),
    "olive": (0.502, 0.502, 0.0),
    "aqua": (0.0, 1.0, 1.0),
    "fuchsia": (1.0, 0.0, 1.0),
}
_PATH_TEXT = "text was exported as paths and the characters are not in the file"
_OUTLINE_NOTE = (
    "Text in this SVG is drawn as outlines and the characters are not in the file. "
    "Re-export with text kept as text, or import the PDF."
)
_IMAGE_ONLY = (
    "this SVG only contains an embedded image. "
    "Import the image or PDF itself to use the photo reader"
)
_NO_VECTOR = "this SVG has no vector geometry"
_PX_SCALE = "sheet scale assumed 96 px/in"
_SCALE = "sheet scale is not an absolute length in the file"
_GLYPHS = {"g", "a", "path", "polygon", "polyline", "use"}


class SvgReadError(ValueError):
    """The file is not an SVG we can read as vectors."""


class Matrix:
    """Affine map x' = a x + c y + e, y' = b x + d y + f."""

    def __init__(self, a: float = 1, b: float = 0, c: float = 0, d: float = 1, e: float = 0, f: float = 0) -> None:
        self.a = float(a)
        self.b = float(b)
        self.c = float(c)
        self.d = float(d)
        self.e = float(e)
        self.f = float(f)

    def mul(self, other: Matrix) -> Matrix:
        """Apply ``other`` first."""

        return Matrix(
            self.a * other.a + self.c * other.b,
            self.b * other.a + self.d * other.b,
            self.a * other.c + self.c * other.d,
            self.b * other.c + self.d * other.d,
            self.a * other.e + self.c * other.f + self.e,
            self.b * other.e + self.d * other.f + self.f,
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def scale_x(self) -> float:
        return math.hypot(self.a, self.b)

    def scale_y(self) -> float:
        return math.hypot(self.c, self.d)

    def uniform(self) -> float:
        return math.sqrt(abs(self.a * self.d - self.b * self.c))


class Frame:
    def __init__(self, ctm: Matrix, vw: float, vh: float) -> None:
        self.ctm = ctm
        self.vw = vw
        self.vh = vh


class _GlyphMark(NamedTuple):
    """One stated glyph. The origin is the pen, and size is the matrix scale.

    ``along_x`` and ``along_y`` are the unit advance. ``page_box`` stays in
    page coordinates when ``box`` and the origin are rotated into a text frame.
    """

    box: tuple[float, float, float, float] | None
    text: str
    origin_x: float
    origin_y: float
    size: float
    glyph_id: str
    along_x: float = 1.0
    along_y: float = 0.0
    page_box: tuple[float, float, float, float] | None = None

    def placed_box(self) -> tuple[float, float, float, float]:
        if self.box is not None:
            return self.box
        return (self.origin_x, self.origin_y, self.origin_x, self.origin_y)


class _Doc:
    def __init__(self) -> None:
        self.tokens: list[dict[str, Any]] = []
        self.strokes: list[dict[str, Any]] = []
        self.rules: list[tuple[Any, dict[str, str], int]] = []
        self.ids: dict[str, ET.Element] = {}
        self.page_w = 1.0
        self.page_h = 1.0
        self._once: set[str] = set()
        self.stack: list[str] = []
        self.real_text = False
        self.saw_image = False
        self.saw_glyphs = False
        self.skip = False
        self.defs_ids: set[str] = set()
        self.use_counts: dict[str, int] = {}
        self.glyph_marks: list[_GlyphMark] = []
        self.pending: list[tuple[tuple[float, float, float, float], list[dict[str, Any]]]] = []
        self.sink: list[dict[str, Any]] | None = None
        # Long non-conductors wait until the width pass. A sheet border that
        # the page's own stroke widths call a wire has to be present for that.
        self.deferred: list[dict[str, Any]] = []

    def note(self, detail: str, x: float | None = None, y: float | None = None, *, once: bool = False) -> None:
        if once and detail in self._once:
            return
        if once:
            self._once.add(detail)
        self.tokens.append(
            _blank_token(self.page_w / 2 if x is None else x, self.page_h / 2 if y is None else y, detail)
        )

    def add_text(self, text: str, x: float, y: float, w: float, h: float) -> None:
        label = str(text or "").strip()
        if not label:
            return
        self.real_text = True
        self.tokens.append(
            {
                "text": label,
                "x": round(x, 2),
                "y": round(y, 2),
                "w": round(max(0.2, w), 2),
                "h": round(max(0.2, h), 2),
            }
        )


def read_svg(path: Path | str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Tokens, strokes, and page size. Vector text and geometry, not a photo."""

    src = Path(path)
    try:
        root = ET.parse(src).getroot()
    except ET.ParseError as exc:
        raise SvgReadError("This file could not be read as an SVG.") from exc
    if _local(root.tag) != "svg":
        raise SvgReadError("This file could not be read as an SVG.")
    doc = _Doc()
    _index(root, doc.ids)
    _mark_defs_and_uses(root, doc)
    _collect_styles(root, doc)
    width, height, frame = _root_frame(root, doc)
    doc.page_w = width
    doc.page_h = height
    if not doc.skip:
        _draw(root, frame, dict(_INITIAL), doc, 0, root=True)
    _finish_glyphs(doc)
    doc.strokes.extend(doc.deferred)
    doc.strokes = finish_conductor_strokes(doc.strokes)
    notice = _svg_notice(doc)
    if notice and not any(str(tok.get("detail") or "") == notice for tok in doc.tokens):
        doc.note(notice, width / 2, height / 2, once=True)
    meta = {
        "width_mm": round(width, 3),
        "height_mm": round(height, 3),
        "rotation": 0,
        "vector": True,
        "text": " ".join(str(tok.get("text") or "") for tok in doc.tokens if tok.get("text")),
        "svg_notice": notice,
    }
    return doc.tokens, doc.strokes, meta


def _blank_token(x: float, y: float, detail: str, w: float = 0.2, h: float = 0.2) -> dict[str, Any]:
    return {
        "text": "",
        "x": round(float(x), 2),
        "y": round(float(y), 2),
        "w": round(max(0.2, w), 2),
        "h": round(max(0.2, h), 2),
        "unreadable": True,
        "reason": "needs_info",
        "detail": detail,
    }


def _svg_notice(doc: _Doc) -> str:
    """Why an SVG produced no devices. Outlined text counts even when wires remain."""

    if doc.saw_image and not doc.strokes and not doc.real_text and not doc.saw_glyphs:
        return _IMAGE_ONLY
    if doc.saw_glyphs and not doc.real_text:
        return _OUTLINE_NOTE
    if not doc.strokes and not doc.real_text and not doc.saw_glyphs:
        return _NO_VECTOR
    return ""


def _local(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _attr(element: ET.Element, name: str) -> str:
    if name in element.attrib:
        return str(element.attrib[name])
    for key, value in element.attrib.items():
        if key == name or key.endswith("}" + name):
            return str(value)
    return ""


def _href(element: ET.Element) -> str:
    return _attr(element, "href") or str(element.attrib.get(_XLINK) or "")


def _floats(text: str) -> list[float]:
    return [float(item) for item in _NUM_RE.findall(text or "")]


def _index(element: ET.Element, ids: dict[str, ET.Element]) -> None:
    eid = _attr(element, "id")
    if eid and eid not in ids:
        ids[eid] = element
    for child in list(element):
        _index(child, ids)


def _mark_defs_and_uses(element: ET.Element, doc: _Doc, *, in_defs: bool = False) -> None:
    local = _local(element.tag)
    if local == "defs":
        in_defs = True
    eid = _attr(element, "id")
    if eid and in_defs:
        doc.defs_ids.add(eid)
    if local == "use":
        ref = _href(element).strip()
        if ref.startswith("#") and ref[1:]:
            doc.use_counts[ref[1:]] = doc.use_counts.get(ref[1:], 0) + 1
    for child in list(element):
        _mark_defs_and_uses(child, doc, in_defs=in_defs)


def _root_frame(root: ET.Element, doc: _Doc) -> tuple[float, float, Frame]:
    raw_box = _attr(root, "viewBox")
    nums = _floats(raw_box)
    view: tuple[float, float, float, float] | None = None
    if raw_box.strip():
        if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
            view = (nums[0], nums[1], nums[2], nums[3])
        else:
            doc.note("viewBox could not be read", once=True)
    w_kind, w_mm = _css_mm(_attr(root, "width"))
    h_kind, h_mm = _css_mm(_attr(root, "height"))
    assumed_px = w_kind == "px" or h_kind == "px"
    if w_kind == "px":
        w_kind = "abs"
    if h_kind == "px":
        h_kind = "abs"
    unclear = w_kind in {"percent", "unknown"} or h_kind in {"percent", "unknown"}
    if view and w_kind == "abs" and h_kind == "missing":
        h_mm = w_mm * view[3] / view[2]
        h_kind = "abs"
    elif view and h_kind == "abs" and w_kind == "missing":
        w_mm = h_mm * view[2] / view[3]
        w_kind = "abs"
    if w_kind != "abs" or h_kind != "abs":
        unclear = True
        if view:
            w_mm = view[2] * PX_MM
            h_mm = view[3] * PX_MM
        else:
            w_mm = 300 * PX_MM
            h_mm = 150 * PX_MM
    if view:
        ctm = _fit(w_mm, h_mm, view[0], view[1], view[2], view[3], _attr(root, "preserveAspectRatio"), doc)
        vw, vh = view[2], view[3]
    else:
        ctm = Matrix(PX_MM, 0, 0, PX_MM, 0, 0)
        vw, vh = w_mm / PX_MM, h_mm / PX_MM
    parsed = _transform_matrix(_attr(root, "transform"), doc)
    if parsed is None:
        doc.skip = True
        doc.note("a transform could not be read", w_mm / 2, h_mm / 2, once=True)
    else:
        ctm = ctm.mul(parsed)
    if assumed_px:
        doc.note(_PX_SCALE, w_mm / 2, h_mm / 2, once=True)
    if unclear:
        doc.note(_SCALE, w_mm / 2, h_mm / 2, once=True)
    return w_mm, h_mm, Frame(ctm, vw, vh)


def _css_mm(raw: str) -> tuple[str, float]:
    text = (raw or "").strip()
    if not text:
        return "missing", 0.0
    if text.endswith("%"):
        try:
            float(text[:-1])
        except ValueError:
            return "unknown", 0.0
        return "percent", 0.0
    match = _LENGTH_RE.match(text)
    if not match:
        return "unknown", 0.0
    value = float(match.group(1))
    unit = match.group(2).lower()
    if not unit:
        return "px", value * _ABS_MM["px"]
    if unit not in _ABS_MM:
        return "unknown", 0.0
    return "abs", value * _ABS_MM[unit]


def _fit(
    vw: float,
    vh: float,
    min_x: float,
    min_y: float,
    vbw: float,
    vbh: float,
    preserve: str,
    doc: _Doc,
) -> Matrix:
    if vbw <= 0 or vbh <= 0 or vw <= 0 or vh <= 0:
        doc.note("viewBox could not be read", once=True)
        return Matrix()
    sx = vw / vbw
    sy = vh / vbh
    align, meet = _aspect(preserve, doc)
    if align == ("none", "none"):
        return Matrix(sx, 0, 0, sy, -min_x * sx, -min_y * sy)
    scale = min(sx, sy) if meet else max(sx, sy)
    ox = {"min": 0.0, "mid": 0.5, "max": 1.0}[align[0]] * (vw - vbw * scale)
    oy = {"min": 0.0, "mid": 0.5, "max": 1.0}[align[1]] * (vh - vbh * scale)
    return Matrix(scale, 0, 0, scale, ox - min_x * scale, oy - min_y * scale)


def _aspect(value: str, doc: _Doc) -> tuple[tuple[str, str], bool]:
    text = (value or "").strip()
    if not text:
        return ("mid", "mid"), True
    if text == "none":
        return ("none", "none"), True
    parts = text.split()
    match = re.fullmatch(r"x(Min|Mid|Max)Y(Min|Mid|Max)", parts[0])
    if not match:
        doc.note("preserveAspectRatio could not be read", once=True)
        return ("mid", "mid"), True
    meet = True
    if len(parts) > 1:
        if parts[1] == "slice":
            meet = False
        elif parts[1] != "meet":
            doc.note("preserveAspectRatio could not be read", once=True)
    return (match.group(1).lower(), match.group(2).lower()), meet


def _collect_styles(root: ET.Element, doc: _Doc) -> None:
    for element in root.iter():
        if _local(element.tag) != "style":
            continue
        _parse_css("".join(element.itertext()), doc)


def _parse_css(text: str, doc: _Doc) -> None:
    body = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    if "@" in body:
        doc.note("a stylesheet at-rule was not applied", once=True)
        body = re.sub(r"@[^{;]*;", " ", body)
        body = re.sub(r"@[^{]*\{[^{}]*\}", " ", body)
    for index, match in enumerate(re.finditer(r"([^{}]+)\{([^{}]*)\}", body)):
        props = _decls(match.group(2))
        if not props:
            continue
        for selector in match.group(1).split(","):
            parsed = _selector(selector.strip())
            if parsed is None:
                doc.note("a stylesheet selector was not applied", once=True)
                continue
            spec = {"id": 100, "class": 10, "tag": 1, "tag-id": 101, "tag-class": 11}[parsed[0]]
            doc.rules.append((parsed, props, spec * 1000 + index))


def _selector(text: str) -> tuple[Any, ...] | None:
    if not text or any(mark in text for mark in " >+~:*[]"):
        return None
    if text.startswith("#") and text.count("#") == 1 and "." not in text:
        return ("id", text[1:])
    if text.startswith(".") and text.count(".") == 1 and "#" not in text:
        return ("class", text[1:])
    if re.fullmatch(r"[A-Za-z][\w-]*", text):
        return ("tag", text)
    hash_split = text.split("#")
    if len(hash_split) == 2 and re.fullmatch(r"[A-Za-z][\w-]*", hash_split[0]) and hash_split[1]:
        return ("tag-id", hash_split[0], hash_split[1])
    dot = text.split(".")
    if len(dot) == 2 and re.fullmatch(r"[A-Za-z][\w-]*", dot[0]) and dot[1]:
        return ("tag-class", dot[0], dot[1])
    return None


def _decls(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in (body or "").split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip().lower()
        value = value.replace("!important", "").strip()
        if key and value:
            found[key] = value
    return found


def _match_rule(selector: tuple[Any, ...], element: ET.Element) -> bool:
    local = _local(element.tag)
    classes = set(_attr(element, "class").split())
    eid = _attr(element, "id")
    kind = selector[0]
    if kind == "id":
        return eid == selector[1]
    if kind == "class":
        return selector[1] in classes
    if kind == "tag":
        return local == selector[1]
    if kind == "tag-id":
        return local == selector[1] and eid == selector[2]
    if kind == "tag-class":
        return local == selector[1] and selector[2] in classes
    return False


def _own_props(element: ET.Element, doc: _Doc) -> dict[str, str]:
    props: dict[str, str] = {}
    for key, value in element.attrib.items():
        name = _local(key).lower()
        if name in _INITIAL:
            props[name] = value.strip()
    matched: list[tuple[int, dict[str, str]]] = []
    for selector, rule, order in doc.rules:
        if _match_rule(selector, element):
            matched.append((order, rule))
    for _order, rule in sorted(matched):
        props.update(rule)
    props.update(_decls(_attr(element, "style")))
    return props


def _font_on_object(element: ET.Element, doc: _Doc) -> bool:
    """Font set on this object, not a document-wide element rule."""

    props: dict[str, str] = {}
    for key in ("font", "font-size", "font-family"):
        value = _attr(element, key).strip()
        if value:
            props[key] = value
    props.update({key: value for key, value in _decls(_attr(element, "style")).items() if key in {"font", "font-size", "font-family"}})
    for selector, rule, _order in doc.rules:
        if selector[0] in {"tag"}:
            continue
        if _match_rule(selector, element):
            for key in ("font", "font-size", "font-family"):
                if rule.get(key):
                    props[key] = rule[key]
    return any(props.get(key) for key in ("font", "font-size", "font-family"))


def _compute(element: ET.Element, parent: dict[str, str], doc: _Doc) -> dict[str, str]:
    style = dict(parent)
    own = _own_props(element, doc)
    _expand_font(own)
    for key, value in own.items():
        if value == "inherit":
            continue
        if value in {"initial", "unset"}:
            if key in _INITIAL:
                style[key] = _INITIAL[key]
            continue
        style[key] = value
    return style


def _expand_font(props: dict[str, str]) -> None:
    shorthand = props.get("font") or ""
    if not shorthand or shorthand in {"inherit", "initial", "unset"}:
        return
    match = re.search(rf"({_NUM}(?:px|pt|pc|mm|cm|in|em|ex)?)", shorthand, flags=re.I)
    if match and "font-size" not in props:
        props["font-size"] = match.group(1)
    if match and "font-family" not in props:
        family = shorthand[match.end() :].strip(" ,")
        if family:
            props["font-family"] = family


def _hidden(style: dict[str, str]) -> bool:
    if str(style.get("display") or "").strip().lower() == "none":
        return True
    return str(style.get("visibility") or "").strip().lower() in {"hidden", "collapse"}


def _user_length(raw: str, frame: Frame, axis: str, doc: _Doc) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        try:
            number = float(text[:-1])
        except ValueError:
            doc.note("a length could not be read", once=True)
            return None
        base = frame.vw if axis == "x" else frame.vh
        return number / 100.0 * base
    match = _LENGTH_RE.match(text)
    if not match:
        doc.note("a length could not be read", once=True)
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if not unit:
        return value
    if unit not in _ABS_MM:
        doc.note("a length unit could not be read", once=True)
        return None
    scale = frame.ctm.scale_x() if axis == "x" else frame.ctm.scale_y()
    if scale < 1e-12:
        doc.note("a length could not be scaled", once=True)
        return None
    return value * _ABS_MM[unit] / scale


def _num_attr(element: ET.Element, name: str, frame: Frame, axis: str, doc: _Doc, default: float | None = None) -> float | None:
    raw = _attr(element, name)
    if not raw.strip():
        return default
    return _user_length(raw, frame, axis, doc)


def _transform_matrix(raw: str, doc: _Doc) -> Matrix | None:
    text = (raw or "").strip()
    if not text:
        return Matrix()
    found = list(re.finditer(r"([A-Za-z]+)\s*\(([^)]*)\)", text))
    if not found:
        doc.note("a transform could not be read", once=True)
        return None
    consumed = re.sub(r"\s+", "", "".join(item.group(0) for item in found))
    if re.sub(r"\s+", "", text) != consumed:
        doc.note("a transform could not be read", once=True)
        return None
    result = Matrix()
    for item in found:
        part = _one_transform(item.group(1), _floats(item.group(2)))
        if part is None:
            doc.note("a transform could not be read", once=True)
            return None
        result = result.mul(part)
    return result


def _one_transform(name: str, nums: list[float]) -> Matrix | None:
    # SVG names are case-sensitive. matrix, translate, scale, rotate, skewX, skewY.
    if name == "matrix" and len(nums) == 6:
        return Matrix(*nums)
    if name == "translate" and len(nums) in {1, 2}:
        return Matrix(1, 0, 0, 1, nums[0], nums[1] if len(nums) == 2 else 0.0)
    if name == "scale" and len(nums) in {1, 2}:
        sy = nums[1] if len(nums) == 2 else nums[0]
        return Matrix(nums[0], 0, 0, sy, 0, 0)
    if name == "rotate" and len(nums) in {1, 3}:
        angle = math.radians(nums[0])
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        rot = Matrix(cos_a, sin_a, -sin_a, cos_a, 0, 0)
        if len(nums) == 1:
            return rot
        return Matrix(1, 0, 0, 1, nums[1], nums[2]).mul(rot).mul(Matrix(1, 0, 0, 1, -nums[1], -nums[2]))
    if name == "skewX" and len(nums) == 1:
        return Matrix(1, 0, math.tan(math.radians(nums[0])), 1, 0, 0)
    if name == "skewY" and len(nums) == 1:
        return Matrix(1, math.tan(math.radians(nums[0])), 0, 1, 0, 0)
    return None


def _color(value: str) -> tuple[float, float, float] | str:
    text = (value or "").strip().lower()
    if text in {"", "none", "transparent"}:
        return "none"
    if text in _NAMED:
        return _NAMED[text]
    if text.startswith("#"):
        hex_text = text[1:]
        if len(hex_text) in {3, 4}:
            hex_text = "".join(ch * 2 for ch in hex_text[:3])
        if len(hex_text) in {6, 8} and re.fullmatch(r"[0-9a-f]+", hex_text):
            return tuple(int(hex_text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        return "unknown"
    match = re.fullmatch(r"rgba?\(([^)]+)\)", text)
    if not match:
        return "unknown"
    parts = [part.strip() for part in match.group(1).replace(",", " ").split()]
    if len(parts) < 3:
        return "unknown"
    try:
        channels = []
        for part in parts[:3]:
            if part.endswith("%"):
                channels.append(max(0.0, min(1.0, float(part[:-1]) / 100.0)))
            else:
                channels.append(max(0.0, min(1.0, float(part) / 255.0)))
    except ValueError:
        return "unknown"
    return (channels[0], channels[1], channels[2])


def _paint(style: dict[str, str], which: str, doc: _Doc) -> tuple[float, float, float] | str | None:
    opacity = _opacity(style.get("opacity")) * _opacity(style.get(f"{which}-opacity"))
    if opacity <= 0:
        return "none"
    raw = str(style.get(which) or ("black" if which == "fill" else "none")).strip()
    if raw == "currentColor":
        raw = str(style.get("color") or "black")
    if raw.startswith("url("):
        doc.note("a paint server was not read", once=True)
        return None
    paint = _color(raw)
    if paint == "unknown":
        doc.note("a color could not be read", once=True)
        return None
    return paint


def _opacity(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


def _draw(
    element: ET.Element,
    frame: Frame,
    parent: dict[str, str],
    doc: _Doc,
    depth: int,
    *,
    root: bool = False,
) -> None:
    if depth > 24:
        doc.note("a symbol reference is too deep", once=True)
        return
    local = _local(element.tag)
    if local in _SKIP or _attr(element, "requiredExtensions") or _attr(element, "requiredFeatures"):
        if _attr(element, "requiredExtensions") or _attr(element, "requiredFeatures"):
            doc.note("an extension the file requires was not read", once=True)
        return
    style = _compute(element, parent, doc)
    if _hidden(style):
        return
    if _attr(element, "clip-path"):
        doc.note("a clip path was not applied", once=True)
    if _attr(element, "mask"):
        doc.note("a mask was not applied", once=True)
    if local == "svg" and not root:
        nested = _nested_frame(element, frame, doc)
        if nested is None:
            return
        for child in list(element):
            _draw(child, nested, style, doc, depth + 1)
        return
    if local == "switch":
        _draw_switch(element, frame, style, doc, depth)
        return
    if local == "use":
        _draw_use(element, frame, style, doc, depth)
        return
    if root:
        transform = Matrix()
    else:
        transform = _transform_matrix(_attr(element, "transform"), doc)
        if transform is None:
            return
    local_frame = Frame(frame.ctm.mul(transform), frame.vw, frame.vh)
    if local == "text":
        _draw_text(element, local_frame, style, doc)
        return
    if local == "foreignObject":
        _draw_foreign(element, local_frame, style, doc)
        return
    if local == "image":
        doc.saw_image = True
        doc.note(_IMAGE_ONLY, once=True)
        return
    if local in {"g", "a"} or (local == "svg" and root):
        if not root and _outlined(element, doc):
            _emit_outlined(element, local_frame, style, doc)
            return
        for child in list(element):
            _draw(child, local_frame, style, doc, depth + 1)
        return
    _emit_shape(element, local_frame, style, doc)


def _nested_frame(element: ET.Element, frame: Frame, doc: _Doc) -> Frame | None:
    x = _num_attr(element, "x", frame, "x", doc, 0.0) or 0.0
    y = _num_attr(element, "y", frame, "y", doc, 0.0) or 0.0
    width = _num_attr(element, "width", frame, "x", doc)
    height = _num_attr(element, "height", frame, "y", doc)
    nums = _floats(_attr(element, "viewBox"))
    view = nums if len(nums) == 4 and nums[2] > 0 and nums[3] > 0 else None
    if _attr(element, "viewBox").strip() and view is None:
        doc.note("viewBox could not be read", once=True)
        return None
    if width is None or height is None:
        if view is None:
            doc.note("a nested svg has no size", once=True)
            return None
        width = view[2] if width is None else width
        height = view[3] if height is None else height
    if view:
        fitted = _fit(width, height, view[0], view[1], view[2], view[3], _attr(element, "preserveAspectRatio"), doc)
        vw, vh = view[2], view[3]
    else:
        fitted = Matrix()
        vw, vh = width, height
    placed = Matrix(1, 0, 0, 1, x, y).mul(fitted)
    transform = _transform_matrix(_attr(element, "transform"), doc)
    if transform is None:
        return None
    return Frame(frame.ctm.mul(transform).mul(placed), vw, vh)


def _draw_switch(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc, depth: int) -> None:
    for child in list(element):
        if _attr(child, "requiredExtensions") or _attr(child, "requiredFeatures"):
            continue
        _draw(child, frame, style, doc, depth + 1)
        return
    doc.note("a switch had no readable branch", once=True)


def _draw_use(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc, depth: int) -> None:
    ref = _href(element).strip()
    if not ref.startswith("#") or not ref[1:]:
        doc.note("a symbol reference was not in the file", once=True)
        return
    target_id = ref[1:]
    target = doc.ids.get(target_id)
    if target is None:
        doc.note("a symbol reference was not in the file", once=True)
        return
    if target_id in doc.stack:
        doc.note("a symbol reference is circular", once=True)
        return
    x = _num_attr(element, "x", frame, "x", doc, 0.0) or 0.0
    y = _num_attr(element, "y", frame, "y", doc, 0.0) or 0.0
    transform = _transform_matrix(_attr(element, "transform"), doc)
    if transform is None:
        return
    ctm = frame.ctm.mul(transform).mul(Matrix(1, 0, 0, 1, x, y))
    if _reusable_glyph(target, target_id, element, style, doc):
        box = _element_box(target, Frame(ctm, frame.vw, frame.vh), style, doc)
        stated = _stated_glyph_text(element)
        small = box is not None and _small_box(box, doc)
        # An empty path is still a glyph. PyMuPDF states a space that way.
        if stated and (box is None or small):
            doc.glyph_marks.append(_glyph_mark(box if small else None, stated, ctm, target_id))
            return
        if small and box is not None:
            doc.glyph_marks.append(_glyph_mark(box, "", ctm, target_id))
            return
    local = _local(target.tag)
    if local in {"symbol", "svg"}:
        width = _num_attr(element, "width", frame, "x", doc)
        height = _num_attr(element, "height", frame, "y", doc)
        nums = _floats(_attr(target, "viewBox"))
        view = nums if len(nums) == 4 and nums[2] > 0 and nums[3] > 0 else None
        if view and (width is None or height is None):
            doc.note("a symbol reference has no size", once=True)
            return
        if view and width is not None and height is not None:
            fitted = _fit(width, height, view[0], view[1], view[2], view[3], _attr(target, "preserveAspectRatio"), doc)
            ctm = ctm.mul(fitted)
            inner = Frame(ctm, view[2], view[3])
        else:
            inner = Frame(ctm, frame.vw, frame.vh)
        doc.stack.append(target_id)
        try:
            for child in list(target):
                _draw(child, inner, style, doc, depth + 1)
        finally:
            doc.stack.pop()
        return
    doc.stack.append(target_id)
    try:
        _draw(target, Frame(ctm, frame.vw, frame.vh), style, doc, depth + 1)
    finally:
        doc.stack.pop()


def _pure_glyphs(element: ET.Element) -> bool:
    saw = False
    for node in element.iter():
        if node is element:
            continue
        local = _local(node.tag)
        if local in {"title", "desc", "metadata"}:
            continue
        if local not in _GLYPHS:
            return False
        if local in {"path", "polygon", "polyline", "use"}:
            saw = True
    return saw


def _direct_text(element: ET.Element, name: str) -> str:
    parts = []
    for child in list(element):
        if _local(child.tag) == name:
            parts.append(" ".join(child.itertext()).strip())
    return " ".join(part for part in parts if part)


def _explicit_labels(element: ET.Element, *, titles: bool) -> list[str]:
    found = []
    for key in ("data-text", "aria-label"):
        value = _attr(element, key).strip()
        if value:
            found.append(value)
    if titles:
        for name in ("title", "desc"):
            value = _direct_text(element, name)
            if value:
                found.append(value)
    return found


def _outlined(element: ET.Element, doc: _Doc) -> bool:
    if _local(element.tag) in {"svg", "text"}:
        return False
    glyphs = _pure_glyphs(element)
    font = _font_on_object(element, doc)
    labels = _explicit_labels(element, titles=glyphs and font)
    if glyphs and (font or labels):
        return True
    return False


def _emit_outlined(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> None:
    glyphs = _pure_glyphs(element)
    font = _font_on_object(element, doc)
    labels = _explicit_labels(element, titles=glyphs and font)
    points = _collect_points(element, frame, style, doc)
    if points:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x = (min(xs) + max(xs)) / 2.0
        y = (min(ys) + max(ys)) / 2.0
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
    else:
        x, y, w, h = frame.ctm.e, frame.ctm.f, 0.2, 0.2
    unique = list(dict.fromkeys(labels))
    if len(unique) > 1:
        doc.note("text labels on one element disagree", x, y)
        return
    if len(unique) == 1:
        doc.add_text(unique[0], x, y, max(w, 0.2), max(h, 0.2))
        return
    doc.tokens.append(_blank_token(x, y, _PATH_TEXT))


def _collect_points(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []

    def visit(node: ET.Element, parent_frame: Frame, parent_style: dict[str, str]) -> None:
        local = _local(node.tag)
        node_style = _compute(node, parent_style, doc)
        transform = _transform_matrix(_attr(node, "transform"), doc)
        if transform is None:
            return
        node_frame = Frame(parent_frame.ctm.mul(transform), parent_frame.vw, parent_frame.vh)
        if local in {"g", "a", "use"}:
            if local == "use":
                _collect_use(node, node_frame, node_style, doc, points)
                return
            for child in list(node):
                visit(child, node_frame, node_style)
            return
        for x, y in _shape_points(node, node_frame, node_style, doc):
            points.append(node_frame.ctm.apply(x, y))

    for child in list(element):
        visit(child, frame, style)
    if _local(element.tag) in {"path", "polygon", "polyline"}:
        for x, y in _shape_points(element, frame, style, doc):
            points.append(frame.ctm.apply(x, y))
    return points


def _collect_use(
    element: ET.Element,
    frame: Frame,
    style: dict[str, str],
    doc: _Doc,
    points: list[tuple[float, float]],
) -> None:
    ref = _href(element).strip()
    target = doc.ids.get(ref[1:]) if ref.startswith("#") else None
    if target is None:
        return
    x = _num_attr(element, "x", frame, "x", doc, 0.0) or 0.0
    y = _num_attr(element, "y", frame, "y", doc, 0.0) or 0.0
    ctm = frame.ctm.mul(Matrix(1, 0, 0, 1, x, y))
    inner = Frame(ctm, frame.vw, frame.vh)
    for px, py in _shape_points(target, inner, style, doc):
        points.append(inner.ctm.apply(px, py))
    for child in list(target):
        if _local(child.tag) in {"path", "polygon", "polyline", "line"}:
            for px, py in _shape_points(child, inner, style, doc):
                points.append(inner.ctm.apply(px, py))


def _shape_points(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> list[tuple[float, float]]:
    local = _local(element.tag)
    if local == "path":
        try:
            ops = _path_ops(_attr(element, "d"))
        except ValueError:
            doc.note("a path could not be read", once=True)
            return []
        return [point for op in ops for point in op[1]]
    if local in {"polygon", "polyline"}:
        try:
            pairs = _pairs(_attr(element, "points"))
        except ValueError:
            doc.note("a path could not be read", once=True)
            return []
        return pairs
    if local == "line":
        x1 = _num_attr(element, "x1", frame, "x", doc, 0.0)
        y1 = _num_attr(element, "y1", frame, "y", doc, 0.0)
        x2 = _num_attr(element, "x2", frame, "x", doc, 0.0)
        y2 = _num_attr(element, "y2", frame, "y", doc, 0.0)
        if None in {x1, y1, x2, y2}:
            return []
        return [(float(x1), float(y1)), (float(x2), float(y2))]
    return []


def _emit_shape(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> None:
    local = _local(element.tag)
    if _attr(element, "marker-start") or _attr(element, "marker-end") or _attr(element, "marker-mid"):
        doc.note("a marker was not read", once=True)
    stroke = _paint(style, "stroke", doc)
    fill = _paint(style, "fill", doc)
    if local in {"line", "polyline"}:
        fill = "none"
    if stroke == "none" and fill == "none":
        return
    paint = stroke if stroke != "none" else fill
    width_raw = style.get("stroke-width") if stroke != "none" else None
    width_user = 0.0 if stroke == "none" else 1.0
    if width_raw is not None:
        parsed = _user_length(str(width_raw), frame, "y", doc)
        if parsed is None:
            return
        width_user = parsed
        if width_user <= 0 and stroke != "none":
            if fill == "none":
                return
            paint = fill
            width_user = 0.0
    vector_effect = _attr(element, "vector-effect") or str(style.get("vector-effect") or "")
    if vector_effect and "non-scaling-stroke" in vector_effect:
        doc.note("a non-scaling stroke width was not applied", once=True)
    if local == "line":
        x1 = _num_attr(element, "x1", frame, "x", doc, 0.0)
        y1 = _num_attr(element, "y1", frame, "y", doc, 0.0)
        x2 = _num_attr(element, "x2", frame, "x", doc, 0.0)
        y2 = _num_attr(element, "y2", frame, "y", doc, 0.0)
        if None in {x1, y1, x2, y2}:
            return
        _stroke_line(doc, frame, (float(x1), float(y1)), (float(x2), float(y2)), paint, width_user, line=True)
        return
    if local == "rect":
        _emit_rect(element, frame, doc, paint, width_user)
        return
    if local in {"circle", "ellipse"}:
        _emit_ellipse(element, frame, doc, paint, width_user, local)
        return
    if local in {"polyline", "polygon"}:
        try:
            pairs = _pairs(_attr(element, "points"))
        except ValueError:
            doc.note("a path could not be read", once=True)
            return
        if local == "polygon" and len(pairs) >= 3:
            pairs = pairs + [pairs[0]]
        start = len(doc.strokes)
        _stroke_chain(doc, frame, pairs, paint, width_user, line=True)
        _hold_filled_run(doc, start, element, style, closed=True)
        return
    if local == "path":
        labels = _explicit_labels(element, titles=False)
        if labels or _font_on_object(element, doc):
            _emit_outlined(element, frame, style, doc)
            return
        try:
            ops = _path_ops(_attr(element, "d"))
        except ValueError:
            doc.note("a path could not be read", once=True)
            return
        start = len(doc.strokes)
        for kind, pts in ops:
            if kind == "line" and len(pts) == 2:
                _stroke_line(doc, frame, pts[0], pts[1], paint, width_user, line=True)
            elif kind == "curve" and len(pts) >= 2:
                _stroke_chain(doc, frame, pts, paint, width_user, line=False)
                for start_pt, end_pt in zip(pts, pts[1:], strict=False):
                    _stroke_line(doc, frame, start_pt, end_pt, paint, width_user, line=True)
        filled = _style_fill_only(style)
        _hold_filled_run(doc, start, element, style, closed=filled or _path_closed(_attr(element, "d")))


def _emit_rect(element: ET.Element, frame: Frame, doc: _Doc, paint: Any, width_user: float) -> None:
    x = _num_attr(element, "x", frame, "x", doc, 0.0)
    y = _num_attr(element, "y", frame, "y", doc, 0.0)
    w = _num_attr(element, "width", frame, "x", doc)
    h = _num_attr(element, "height", frame, "y", doc)
    if None in {x, y, w, h} or w <= 0 or h <= 0:
        return
    rx = _num_attr(element, "rx", frame, "x", doc, 0.0) or 0.0
    ry = _num_attr(element, "ry", frame, "y", doc, 0.0) or 0.0
    if _attr(element, "rx") and not _attr(element, "ry"):
        ry = rx
    if _attr(element, "ry") and not _attr(element, "rx"):
        rx = ry
    rx = min(max(rx, 0.0), w / 2.0)
    ry = min(max(ry, 0.0), h / 2.0)
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    mapped = [frame.ctm.apply(px, py) for px, py in corners]
    if rx <= 0 or ry <= 0:
        _stroke_chain(doc, frame, corners + [corners[0]], paint, width_user, line=True, from_rect=True)
        _maybe_rect(doc, frame, mapped, paint, width_user)
        return
    _rounded(doc, frame, x, y, w, h, rx, ry, paint, width_user)
    _maybe_rect(doc, frame, mapped, paint, width_user)


def _rounded(
    doc: _Doc,
    frame: Frame,
    x: float,
    y: float,
    w: float,
    h: float,
    rx: float,
    ry: float,
    paint: Any,
    width_user: float,
) -> None:
    edges = [
        ((x + rx, y), (x + w - rx, y)),
        ((x + w, y + ry), (x + w, y + h - ry)),
        ((x + w - rx, y + h), (x + rx, y + h)),
        ((x, y + h - ry), (x, y + ry)),
    ]
    arcs = [
        (x + w - rx, y, x + w, y + ry),
        (x + w, y + h - ry, x + w - rx, y + h),
        (x + rx, y + h, x, y + h - ry),
        (x, y + ry, x + rx, y),
    ]
    for start, end in edges:
        _stroke_line(doc, frame, start, end, paint, width_user, line=True, from_rect=True)
    for x1, y1, x2, y2 in arcs:
        samples = _arc_points(x1, y1, rx, ry, 0, 0, 1, x2, y2)
        if len(samples) >= 2:
            _stroke_chain(doc, frame, samples, paint, width_user, line=False)
            for a, b in zip(samples, samples[1:], strict=False):
                _stroke_line(doc, frame, a, b, paint, width_user, line=True, from_rect=True)


def _emit_ellipse(element: ET.Element, frame: Frame, doc: _Doc, paint: Any, width_user: float, local: str) -> None:
    cx = _num_attr(element, "cx", frame, "x", doc, 0.0)
    cy = _num_attr(element, "cy", frame, "y", doc, 0.0)
    if local == "circle":
        radius = _num_attr(element, "r", frame, "x", doc)
        rx = ry = radius
    else:
        rx = _num_attr(element, "rx", frame, "x", doc)
        ry = _num_attr(element, "ry", frame, "y", doc)
    if None in {cx, cy, rx, ry} or rx <= 0 or ry <= 0:
        return
    points = []
    for step in range(24):
        angle = 2.0 * math.pi * step / 24.0
        points.append((cx + rx * math.cos(angle), cy + ry * math.sin(angle)))
    points.append(points[0])
    _stroke_chain(doc, frame, points, paint, width_user, line=False)


def _maybe_rect(
    doc: _Doc,
    frame: Frame,
    mapped: list[tuple[float, float]],
    paint: Any,
    width_user: float,
) -> None:
    xs = [point[0] for point in mapped]
    ys = [point[1] for point in mapped]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if omit_border_rect(width, height):
        return
    if not _axis_box(mapped):
        return
    role = _paint_role(paint, _width_pt(frame, width_user), line=False)
    _push_stroke(
        doc, {"kind": "rect", "role": role, "x": _r(min(xs)), "y": _r(min(ys)), "w": _r(width), "h": _r(height)}
    )


def _axis_box(points: list[tuple[float, float]]) -> bool:
    if len(points) != 4:
        return False
    for start, end in zip(points, points[1:] + points[:1], strict=False):
        if abs(start[0] - end[0]) > 0.05 and abs(start[1] - end[1]) > 0.05:
            return False
    return True


def _stroke_chain(
    doc: _Doc,
    frame: Frame,
    points: list[tuple[float, float]],
    paint: Any,
    width_user: float,
    *,
    line: bool,
    from_rect: bool = False,
) -> None:
    if len(points) < 2:
        return
    if not line:
        mapped = [frame.ctm.apply(x, y) for x, y in points]
        role = _paint_role(paint, _width_pt(frame, width_user), line=False)
        _push_stroke(doc, {"kind": "curve", "role": role, "points": [[_r(x), _r(y)] for x, y in mapped]})
        return
    for start, end in zip(points, points[1:], strict=False):
        _stroke_line(doc, frame, start, end, paint, width_user, line=True, from_rect=from_rect)


def _stroke_line(
    doc: _Doc,
    frame: Frame,
    start: tuple[float, float],
    end: tuple[float, float],
    paint: Any,
    width_user: float,
    *,
    line: bool,
    from_rect: bool = False,
) -> None:
    a = frame.ctm.apply(start[0], start[1])
    b = frame.ctm.apply(end[0], end[1])
    if math.hypot(a[0] - b[0], a[1] - b[1]) < 0.05:
        return
    width_pt = _width_pt(frame, width_user)
    color = paint if isinstance(paint, tuple) else None
    # A rectangle edge is symbol ink. The fixed black-phase width must not
    # turn the box into a conductor; the page-width pass skips it too.
    role = _role(color, width_pt) if from_rect else _paint_role(paint, width_pt, line=line)
    pa, pb = [_r(a[0]), _r(a[1])], [_r(b[0]), _r(b[1])]
    stroke = {
        "kind": "line",
        "role": role,
        "a": pa,
        "b": pb,
        "pt": float(width_pt or 0),
        "color": color,
        "span": _span(pa, pb),
    }
    if from_rect:
        stroke["from_rect"] = True
    if omit_border_line(role, pa, pb):
        # Keep it for the width pass unless a glyph run is collecting strokes.
        # Adding it now would shift the indexes that group those outlines.
        if doc.sink is None:
            doc.deferred.append(stroke)
        return
    _push_stroke(doc, stroke)


def _push_stroke(doc: _Doc, stroke: dict[str, Any]) -> None:
    if doc.sink is not None:
        doc.sink.append(stroke)
    else:
        doc.strokes.append(stroke)


def _width_pt(frame: Frame, width_user: float) -> float | None:
    if width_user <= 0:
        return None
    return width_user * frame.ctm.uniform() * PT_PER_MM


def _paint_role(paint: Any, width_pt: float | None, *, line: bool) -> str:
    color = paint if isinstance(paint, tuple) else None
    if line and color is not None and width_pt is not None:
        return stroke_role(color, width_pt)
    return _role(color, width_pt)


def _style_fill_only(style: dict[str, str]) -> bool:
    stroke = str(style.get("stroke") or "none").strip().lower()
    fill = str(style.get("fill") or "black").strip().lower()
    if stroke not in {"", "none", "transparent"}:
        return False
    return fill not in {"", "none", "transparent"}


def _path_closed(data: str) -> bool:
    """True when every drawn subpath is closed.

    A moveto that is not followed by a drawing command does not count.
    Cairo glyph paths end with that extra moveto after the last close.
    """

    if not data:
        return False
    moves = 0
    closes = 0
    pending = False
    drawn = False
    for command in re.findall(r"[A-Za-z]", data):
        if command in "Mm":
            if pending and drawn:
                moves += 1
            pending = True
            drawn = False
        elif command in "Zz":
            if pending:
                moves += 1
                pending = False
            closes += 1
            drawn = False
        elif command in "LlHhVvCcSsQqTtAa":
            drawn = True
    if pending and drawn:
        moves += 1
    if moves == 0:
        return False
    return closes >= moves


def _hold_filled_run(doc: _Doc, start: int, element: ET.Element, style: dict[str, str], *, closed: bool) -> None:
    """A small filled outline with no stroke is a glyph, not a wire."""

    if not closed or not _style_fill_only(style):
        return
    fresh = doc.strokes[start:]
    box = _box_of_strokes(fresh)
    if not fresh or box is None or not _small_box(box, doc):
        return
    del doc.strokes[start:]
    doc.pending.append((box, fresh))


def _box_of_strokes(strokes: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for stroke in strokes:
        if stroke.get("kind") == "line":
            xs.extend((float(stroke["a"][0]), float(stroke["b"][0])))
            ys.extend((float(stroke["a"][1]), float(stroke["b"][1])))
        elif stroke.get("kind") == "curve":
            for point in stroke.get("points") or []:
                xs.append(float(point[0]))
                ys.append(float(point[1]))
        elif stroke.get("kind") == "rect":
            x, y = float(stroke["x"]), float(stroke["y"])
            xs.extend((x, x + float(stroke["w"])))
            ys.extend((y, y + float(stroke["h"])))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _small_box(box: tuple[float, float, float, float], doc: _Doc) -> bool:
    """Small next to the sheet. A letter is; a filled frame is not."""

    width = box[2] - box[0]
    height = box[3] - box[1]
    limit = 0.2 * min(doc.page_w, doc.page_h)
    if width <= 0 or height <= 0 or limit <= 0:
        return False
    return width <= limit and height <= limit


def _stated_glyph_text(element: ET.Element) -> str:
    """The character a glyph use states. Disagreeing labels are not a guess.

    A whitespace-only value is a space. PyMuPDF writes one for every word space.
    """

    found: list[str] = []
    for key in ("data-text", "aria-label"):
        raw = _attr(element, key)
        if not raw:
            continue
        value = raw.strip()
        found.append(value if value else " ")
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0]
    return ""


def _reusable_glyph(
    target: ET.Element, target_id: str, use: ET.Element, style: dict[str, str], doc: _Doc
) -> bool:
    if target_id not in doc.defs_ids and doc.use_counts.get(target_id, 0) < 2:
        return False
    if _stated_glyph_text(use) and _path_only(target, style, doc):
        return True
    return _fill_only_closed(target, style, doc)


def _path_only(element: ET.Element, style: dict[str, str], doc: _Doc) -> bool:
    """A glyph def is paths (or polygons) only. A symbol made of lines is not."""

    saw = False

    def walk(node: ET.Element, parent: dict[str, str]) -> bool:
        nonlocal saw
        node_style = _compute(node, parent, doc)
        local = _local(node.tag)
        if local in {"title", "desc", "metadata"}:
            return True
        if local in {"g", "a"}:
            return all(walk(child, node_style) for child in list(node))
        if local in {"path", "polygon"}:
            saw = True
            return True
        return False

    return walk(element, style) and saw


def _fill_only_closed(element: ET.Element, style: dict[str, str], doc: _Doc) -> bool:
    """A fill paints a closed shape. SVG closes a filled subpath even without Z."""

    saw = False

    def walk(node: ET.Element, parent: dict[str, str]) -> bool:
        nonlocal saw
        node_style = _compute(node, parent, doc)
        local = _local(node.tag)
        if local in {"title", "desc", "metadata"}:
            return True
        if local in {"g", "a"}:
            return all(walk(child, node_style) for child in list(node))
        if local == "path":
            if not _style_fill_only(node_style):
                return False
            saw = True
            return True
        if local == "polygon":
            if not _style_fill_only(node_style):
                return False
            saw = True
            return True
        return False

    return walk(element, style) and saw


def _element_box(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> tuple[float, float, float, float] | None:
    points = _collect_points(element, frame, style, doc)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _finish_glyphs(doc: _Doc) -> None:
    labeled = [mark for mark in doc.glyph_marks if mark.text]
    unlabeled = [mark.placed_box() for mark in doc.glyph_marks if not mark.text and mark.box is not None]
    for run in _baseline_runs(unlabeled, min_count=1, page_w=doc.page_w):
        _note_glyph_run(doc, run)
    for text, x, y, width, height in _glyph_words(labeled, doc.page_w):
        doc.add_text(text, x, y, width, height)
    pending_boxes = [box for box, _strokes in doc.pending]
    runs = _baseline_runs(pending_boxes, min_count=2, page_w=doc.page_w)
    used: set[int] = set()
    for run in runs:
        _note_glyph_run(doc, run)
        for box in run:
            for index, (pending, _strokes) in enumerate(doc.pending):
                if pending == box:
                    used.add(index)
    for index, (_box, strokes) in enumerate(doc.pending):
        if index not in used:
            doc.strokes.extend(strokes)
    doc.pending.clear()


def _glyph_mark(
    box: tuple[float, float, float, float] | None, text: str, ctm: Matrix, glyph_id: str
) -> _GlyphMark:
    scale = ctm.scale_x() or 1.0
    return _GlyphMark(box, text, ctm.e, ctm.f, scale, glyph_id, ctm.a / scale, ctm.b / scale, box)


def _direction_key(mark: _GlyphMark) -> int:
    """Advance direction in whole degrees. 359° and 1° share a bucket."""

    deg = math.degrees(math.atan2(mark.along_y, mark.along_x)) % 360.0
    return int(round(deg)) % 360


def _in_text_frame(mark: _GlyphMark, direction: int) -> _GlyphMark:
    """Pen and ink in a frame whose x axis is this text's advance."""

    theta = math.radians(direction)
    ux, uy = math.cos(theta), math.sin(theta)
    vx, vy = -uy, ux
    origin_u = mark.origin_x * ux + mark.origin_y * uy
    origin_v = mark.origin_x * vx + mark.origin_y * vy
    page_box = mark.page_box if mark.page_box is not None else mark.box
    if page_box is None:
        projected = None
    else:
        corners = (
            (page_box[0], page_box[1]),
            (page_box[2], page_box[1]),
            (page_box[0], page_box[3]),
            (page_box[2], page_box[3]),
        )
        us = [x * ux + y * uy for x, y in corners]
        vs = [x * vx + y * vy for x, y in corners]
        projected = (min(us), min(vs), max(us), max(vs))
    return mark._replace(box=projected, origin_x=origin_u, origin_y=origin_v, page_box=page_box)


def _glyph_words(marks: list[_GlyphMark], page_w: float) -> list[tuple[str, float, float, float, float]]:
    """Join stated glyphs that share an advance direction.

    Horizontal text stays in page coordinates. A vertical label is rotated
    into its own frame, so it never joins a horizontal word.
    """

    if not marks:
        return []
    buckets: dict[int, list[_GlyphMark]] = {}
    for mark in marks:
        buckets.setdefault(_direction_key(mark), []).append(mark)
    words: list[tuple[str, float, float, float, float]] = []
    for direction in sorted(buckets):
        framed = [_in_text_frame(mark, direction) for mark in buckets[direction]]
        words.extend(_aligned_glyph_words(framed, page_w))
    return words


def _aligned_glyph_words(
    marks: list[_GlyphMark], page_w: float
) -> list[tuple[str, float, float, float, float]]:
    """Join glyphs already expressed along their advance.

    The pen origin steps by each glyph's advance. A word space is an extra
    0.2 em or more on that step. A gap between ink boxes is only a fallback,
    and only when it is wider than both 0.3 em and the run's letter gaps.
    """

    if not marks:
        return []
    lines = _pen_baseline_lines(marks, page_w)
    advances = _glyph_advances(lines)
    words: list[tuple[str, float, float, float, float]] = []
    for line in lines:
        ordered = sorted(line, key=lambda mark: (mark.origin_x, mark.origin_y))
        ems = _ems_by_size(ordered)
        gaps = [_glyph_ink_gap(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)]
        limits = {
            key: _word_gap_limit(
                [gaps[i] for i in range(len(gaps)) if _size_key(ordered[i]) == key],
                em,
            )
            for key, em in ems.items()
        }
        chunks: list[list[_GlyphMark]] = []
        for offset, mark in enumerate(ordered):
            if not mark.text.strip():
                chunks.append([])
                continue
            if not chunks or not chunks[-1]:
                chunks.append([mark])
                continue
            prev = chunks[-1][-1]
            key = _size_key(prev)
            if _glyph_word_break(prev, mark, gaps[offset - 1], advances, limits[key], ems[key]):
                chunks.append([mark])
            else:
                chunks[-1].append(mark)
        for chunk in chunks:
            if not chunk:
                continue
            text = re.sub(r"\s+", " ", "".join(mark.text for mark in chunk)).strip()
            if not text:
                continue
            ink = [mark.page_box for mark in chunk if mark.page_box is not None]
            boxes_in = ink or [mark.placed_box() for mark in chunk]
            x0 = min(box[0] for box in boxes_in)
            y0 = min(box[1] for box in boxes_in)
            x1 = max(box[2] for box in boxes_in)
            y1 = max(box[3] for box in boxes_in)
            words.append((text, (x0 + x1) / 2.0, (y0 + y1) / 2.0, max(x1 - x0, 0.2), max(y1 - y0, 0.2)))
    return words


def _pen_baseline_lines(marks: list[_GlyphMark], page_w: float) -> list[list[_GlyphMark]]:
    """Stated glyphs share a line when their pen origins share a baseline.

    The ink bottom is not the baseline: a star, quote, or degree mark sits
    above it, and a descender sits below it. Two baselines a fraction of an
    em apart are two lines. A wide horizontal gap is still a different run.
    """

    if not marks:
        return []
    ems = [_line_em([mark]) for mark in marks]
    order = sorted(range(len(marks)), key=lambda index: (marks[index].origin_y, marks[index].origin_x))
    rows: list[list[int]] = []
    for index in order:
        if rows:
            row = rows[-1]
            base = sorted(marks[item].origin_y for item in row)[len(row) // 2]
            tol = 0.15 * max(max(ems[item] for item in row), ems[index])
            if abs(marks[index].origin_y - base) <= tol:
                row.append(index)
                continue
        rows.append([index])
    lines: list[list[_GlyphMark]] = []
    for row in rows:
        row.sort(key=lambda index: marks[index].origin_x)
        boxes = [marks[index].placed_box() for index in row]
        run = [row[0]]
        for cursor in range(1, len(row)):
            previous = boxes[cursor - 1]
            current = boxes[cursor]
            gap = _x_gap(previous, current) if previous[2] < current[0] else 0.0
            widths = sorted(marks[index].placed_box()[2] - marks[index].placed_box()[0] for index in run)
            median = widths[len(widths) // 2]
            if gap > max(8.0 * median, 0.15 * page_w):
                lines.append([marks[index] for index in run])
                run = []
            run.append(row[cursor])
        lines.append([marks[index] for index in run])
    return lines


def _size_key(mark: _GlyphMark) -> float:
    return round(mark.size if mark.size > 0 else 1.0, 3)


def _ems_by_size(marks: list[_GlyphMark]) -> dict[float, float]:
    """One em for each matrix scale on the line. A small label and a larger tag do not share it."""

    groups: dict[float, list[_GlyphMark]] = {}
    for mark in marks:
        groups.setdefault(_size_key(mark), []).append(mark)
    return {key: _line_em(group) for key, group in groups.items()}


def _line_em(marks: list[_GlyphMark]) -> float:
    """Page units of one em for glyphs that share a matrix scale.

    The use matrix is the font size when the glyph def is in font units.
    A def already drawn at sheet size is many units tall and the matrix is
    often 1; the ink height is the em then. Cap height is about 0.7 em.
    """

    heights = sorted(
        mark.box[3] - mark.box[1] for mark in marks if mark.box is not None and mark.box[3] > mark.box[1]
    )
    ink = heights[len(heights) // 2] if heights else 0.0
    scales = sorted(mark.size for mark in marks if mark.size > 0)
    scale = scales[len(scales) // 2] if scales else 1.0
    if scale <= 0:
        scale = 1.0
    if ink > 0 and ink / scale > 1.5:
        return max(scale, ink / 0.7)
    return scale


def _glyph_advances(lines: list[list[_GlyphMark]]) -> dict[str, float]:
    """Pen advance of each glyph def, in em.

    A step that lands on a stated space is that glyph's advance. Space marks
    themselves are not samples. The tighter origin steps are the advance.
    When every step already sits a word space past the ink, the outline is
    the advance instead.
    """

    steps: dict[str, list[float]] = {}
    outlines: dict[str, list[float]] = {}
    for line in lines:
        ordered = sorted(line, key=lambda mark: mark.origin_x)
        ems = _ems_by_size(ordered)
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if not prev.text.strip() or not prev.glyph_id:
                continue
            em = ems[_size_key(prev)]
            step = (nxt.origin_x - prev.origin_x) / em
            if step <= 0:
                continue
            steps.setdefault(prev.glyph_id, []).append(step)
            outline = _outline_em(prev, em)
            if outline > 0:
                outlines.setdefault(prev.glyph_id, []).append(outline)
    advances: dict[str, float] = {}
    for glyph_id, samples in steps.items():
        samples.sort()
        half = samples[: max(1, (len(samples) + 1) // 2)]
        learned = half[len(half) // 2]
        outline_samples = outlines.get(glyph_id) or []
        outline = 0.0
        if outline_samples:
            outline_samples.sort()
            outline = outline_samples[len(outline_samples) // 2]
        # Side bearings stay under a word space. A pitch that clears the ink
        # by more than that is the advance plus a space.
        if outline > 0 and learned - outline >= 0.26:
            learned = outline
        advances[glyph_id] = learned
    return advances


def _outline_em(mark: _GlyphMark, em: float) -> float:
    """Right edge of the ink, in em, measured from the pen origin."""

    if mark.box is None or em <= 0:
        return 0.0
    return max(0.0, (mark.box[2] - mark.origin_x) / em)


def _glyph_word_break(
    prev: _GlyphMark, mark: _GlyphMark, ink_gap: float, advances: dict[str, float], limit: float, em: float
) -> bool:
    step_em = (mark.origin_x - prev.origin_x) / em if em > 0 else 0.0
    advance = advances.get(prev.glyph_id)
    if advance is not None:
        return step_em - advance >= 0.20
    return ink_gap > limit


def _glyph_ink_gap(left: _GlyphMark, right: _GlyphMark) -> float:
    if left.box is not None and right.box is not None:
        return _x_gap(left.box, right.box)
    return max(0.0, right.origin_x - left.origin_x)


def _word_gap_limit(gaps: list[float], size: float) -> float:
    """Ink gap that starts a word when the pen advance is unknown.

    The floor is 0.3 em. The letter spacing is the tighter half of the gaps
    in the run, and a word space is at least two and a half times that.
    """

    if size <= 0:
        size = 1.0
    floor = 0.30 * size
    positive = sorted(gap for gap in gaps if gap > 0)
    if not positive:
        return floor
    half = positive[: max(1, (len(positive) + 1) // 2)]
    typical = half[len(half) // 2]
    return max(floor, 2.5 * typical)


def _baseline_index_groups(
    boxes: list[tuple[float, float, float, float]], *, min_count: int, page_w: float
) -> list[list[int]]:
    order = sorted(range(len(boxes)), key=lambda index: ((boxes[index][1] + boxes[index][3]) / 2.0, boxes[index][0]))
    used = [False] * len(boxes)
    groups: list[list[int]] = []
    for index in order:
        if used[index]:
            continue
        group = [index]
        used[index] = True
        grew = True
        while grew:
            grew = False
            for other in order:
                if used[other] or not _joins_baseline(boxes, group, other, page_w):
                    continue
                group.append(other)
                used[other] = True
                grew = True
        if len(group) >= min_count:
            groups.append(group)
    return groups


def _note_glyph_run(doc: _Doc, boxes: list[tuple[float, float, float, float]]) -> None:
    if not boxes:
        return
    x0 = min(box[0] for box in boxes)
    y0 = min(box[1] for box in boxes)
    x1 = max(box[2] for box in boxes)
    y1 = max(box[3] for box in boxes)
    doc.saw_glyphs = True
    doc.tokens.append(_blank_token((x0 + x1) / 2.0, (y0 + y1) / 2.0, _PATH_TEXT, w=x1 - x0, h=y1 - y0))


def _baseline_runs(
    boxes: list[tuple[float, float, float, float]], *, min_count: int, page_w: float
) -> list[list[tuple[float, float, float, float]]]:
    """Glyphs that share a baseline. A wide gap is a different run."""

    return [
        [boxes[index] for index in group]
        for group in _baseline_index_groups(boxes, min_count=min_count, page_w=page_w)
    ]


def _joins_baseline(boxes: list[tuple[float, float, float, float]], group: list[int], other: int, page_w: float) -> bool:
    box = boxes[other]
    base = sorted(boxes[item][3] for item in group)[len(group) // 2]
    height = max(boxes[item][3] - boxes[item][1] for item in group)
    if abs(box[3] - base) > 0.45 * max(height, box[3] - box[1], 0.1):
        return False
    gap = min(_x_gap(box, boxes[item]) for item in group)
    widths = sorted(boxes[item][2] - boxes[item][0] for item in group)
    median = widths[len(widths) // 2]
    return gap <= max(8.0 * median, 0.15 * page_w)


def _x_gap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    if left[2] < right[0]:
        return right[0] - left[2]
    if right[2] < left[0]:
        return left[0] - right[2]
    return 0.0


def _pairs(text: str) -> list[tuple[float, float]]:
    nums = _floats(text)
    if len(nums) < 2 or len(nums) % 2:
        raise ValueError("points")
    return list(zip(nums[0::2], nums[1::2], strict=False))


def _path_tokens(data: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(data):
        char = data[index]
        if char in " \t\r\n,":
            index += 1
            continue
        if char.isalpha():
            tokens.append(char)
            index += 1
            continue
        match = _NUM_RE.match(data, index)
        if not match:
            raise ValueError(data[index : index + 8])
        tokens.append(match.group(0))
        index = match.end()
    return tokens


def _path_ops(data: str) -> list[tuple[str, list[tuple[float, float]]]]:
    if not (data or "").strip():
        return []
    tokens = _path_tokens(data)
    index = 0
    command = ""
    cx = cy = sx = sy = 0.0
    prev = ""
    ctrl: tuple[float, float] | None = None
    ops: list[tuple[str, list[tuple[float, float]]]] = []

    def take(count: int) -> list[float]:
        nonlocal index
        if index + count > len(tokens):
            raise ValueError("short")
        values = []
        for _ in range(count):
            token = tokens[index]
            if token.isalpha():
                raise ValueError("short")
            values.append(float(token))
            index += 1
        return values

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
        if not command:
            raise ValueError("command")
        relative = command.islower()
        if command in "Zz":
            ops.append(("line", [(cx, cy), (sx, sy)]))
            cx, cy = sx, sy
            ctrl = None
            prev = "Z"
            command = ""
            continue
        if command in "Mm":
            x, y = take(2)
            if relative:
                x += cx
                y += cy
            ops.append(("line", [(x, y), (x, y)]))
            cx, cy = sx, sy = x, y
            ctrl = None
            prev = "M"
            command = "l" if relative else "L"
            continue
        if command in "Ll":
            x, y = take(2)
            if relative:
                x += cx
                y += cy
            ops.append(("line", [(cx, cy), (x, y)]))
            cx, cy = x, y
            ctrl = None
            prev = "L"
            continue
        if command in "Hh":
            (x,) = take(1)
            if relative:
                x += cx
            ops.append(("line", [(cx, cy), (x, cy)]))
            cx = x
            ctrl = None
            prev = "L"
            continue
        if command in "Vv":
            (y,) = take(1)
            if relative:
                y += cy
            ops.append(("line", [(cx, cy), (cx, y)]))
            cy = y
            ctrl = None
            prev = "L"
            continue
        if command in "Cc":
            x1, y1, x2, y2, x, y = take(6)
            if relative:
                x1 += cx
                y1 += cy
                x2 += cx
                y2 += cy
                x += cx
                y += cy
            samples = _flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x, y))
            ops.append(("curve", samples))
            ctrl = (x2, y2)
            cx, cy = x, y
            prev = "C"
            continue
        if command in "Ss":
            x2, y2, x, y = take(4)
            if prev == "C" and ctrl is not None:
                x1, y1 = 2 * cx - ctrl[0], 2 * cy - ctrl[1]
            else:
                x1, y1 = cx, cy
            if relative:
                x2 += cx
                y2 += cy
                x += cx
                y += cy
            samples = _flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x, y))
            ops.append(("curve", samples))
            ctrl = (x2, y2)
            cx, cy = x, y
            prev = "C"
            continue
        if command in "Qq":
            qx, qy, x, y = take(4)
            if relative:
                qx += cx
                qy += cy
                x += cx
                y += cy
            samples = _flatten_cubic((cx, cy), *_quad(cx, cy, qx, qy, x, y), (x, y))
            ops.append(("curve", samples))
            ctrl = (qx, qy)
            cx, cy = x, y
            prev = "Q"
            continue
        if command in "Tt":
            x, y = take(2)
            if prev == "Q" and ctrl is not None:
                qx, qy = 2 * cx - ctrl[0], 2 * cy - ctrl[1]
            else:
                qx, qy = cx, cy
            if relative:
                x += cx
                y += cy
            samples = _flatten_cubic((cx, cy), *_quad(cx, cy, qx, qy, x, y), (x, y))
            ops.append(("curve", samples))
            ctrl = (qx, qy)
            cx, cy = x, y
            prev = "Q"
            continue
        if command in "Aa":
            rx, ry, phi, large, sweep, x, y = take(7)
            if relative:
                x += cx
                y += cy
            samples = _arc_points(cx, cy, rx, ry, phi, int(large), int(sweep), x, y)
            if len(samples) >= 2:
                ops.append(("curve", samples))
            else:
                ops.append(("line", [(cx, cy), (x, y)]))
            cx, cy = x, y
            ctrl = None
            prev = "A"
            continue
        raise ValueError(command)
    # A bare moveto is not a stroke.
    return [op for op in ops if not (op[0] == "line" and op[1][0] == op[1][1])]


def _quad(
    cx: float, cy: float, qx: float, qy: float, x: float, y: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    c1 = (cx + 2.0 / 3.0 * (qx - cx), cy + 2.0 / 3.0 * (qy - cy))
    c2 = (x + 2.0 / 3.0 * (qx - x), y + 2.0 / 3.0 * (qy - y))
    return c1, c2


def _flatten_cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = [p0]

    def rec(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float], depth: int) -> None:
        if depth > 8 or (_seg_gap(b, a, d) <= 0.2 and _seg_gap(c, a, d) <= 0.2):
            points.append(d)
            return
        ab = _mid(a, b)
        bc = _mid(b, c)
        cd = _mid(c, d)
        abc = _mid(ab, bc)
        bcd = _mid(bc, cd)
        mid = _mid(abc, bcd)
        rec(a, ab, abc, mid, depth + 1)
        rec(mid, bcd, cd, d, depth + 1)

    rec(p0, p1, p2, p3, 0)
    return points


def _mid(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _seg_gap(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = dx * dx + dy * dy
    if length <= 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length))
    return math.hypot(point[0] - (start[0] + t * dx), point[1] - (start[1] + t * dy))


def _arc_points(
    x1: float,
    y1: float,
    rx: float,
    ry: float,
    phi_deg: float,
    large: int,
    sweep: int,
    x2: float,
    y2: float,
) -> list[tuple[float, float]]:
    if math.hypot(x2 - x1, y2 - y1) < 1e-9:
        return [(x2, y2)]
    if abs(rx) < 1e-9 or abs(ry) < 1e-9:
        return [(x1, y1), (x2, y2)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale
    sign = -1.0 if bool(large) == bool(sweep) else 1.0
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = sign * math.sqrt(max(0.0, num / den)) if den else 0.0
    cxp = coef * (rx * y1p / ry)
    cyp = coef * -(ry * x1p / rx)
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        if norm == 0:
            return 0.0
        ang = math.acos(max(-1.0, min(1.0, dot / norm)))
        if ux * vy - uy * vx < 0:
            ang = -ang
        return ang

    theta = angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    steps = max(4, min(96, int(abs(delta) / math.radians(8.0)) or 1))
    points = [(x1, y1)]
    for step in range(1, steps + 1):
        t = theta + delta * step / steps
        cos_t, sin_t = math.cos(t), math.sin(t)
        x = cx + rx * cos_t * cos_p - ry * sin_t * sin_p
        y = cy + rx * cos_t * sin_p + ry * sin_t * cos_p
        points.append((x, y))
    points[-1] = (x2, y2)
    return points


def _draw_text(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> None:
    runs = _text_runs(element, frame, style, doc)
    if runs:
        for text, x, y, size, anchor, baseline, width in runs:
            _place_run(text, x, y, size, anchor, baseline, frame, doc, width=width)
        return
    if any(_local(node.tag) in {"path", "polygon", "polyline", "use"} for node in element.iter()):
        _emit_outlined(element, frame, style, doc)


def _draw_foreign(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> None:
    text = re.sub(r"\s+", " ", " ".join(element.itertext())).strip()
    if not text:
        return
    x = _num_attr(element, "x", frame, "x", doc)
    y = _num_attr(element, "y", frame, "y", doc)
    if x is None or y is None:
        doc.note("text in a foreign object has no position", once=True)
        return
    size = _font_user(style, frame, doc) or 3.0
    _place_run(text, x, y + size, size, "start", "auto", frame, doc)


def _text_runs(
    element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc
) -> list[tuple[str, float, float, float, str, str, float]]:
    """One entry per word. Per-character x lists and spaces both split the run."""

    raws = _raw_chars(element, frame, style, doc, is_root=True)
    placed = _place_chars(raws)
    return _group_words(placed)


def _text_on_path(element: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc) -> None:
    text = _collapse("".join(element.itertext()), False)
    if not text:
        return
    ref = _href(element).strip()
    target = doc.ids.get(ref[1:]) if ref.startswith("#") else None
    if target is None:
        doc.note("text on a path had no path", once=True)
        return
    try:
        ops = _path_ops(_attr(target, "d"))
    except ValueError:
        doc.note("text on a path had no path", once=True)
        return
    points = [point for _kind, pts in ops for point in pts]
    if not points:
        doc.note("text on a path had no path", once=True)
        return
    transform = _transform_matrix(_attr(target, "transform"), doc) or Matrix()
    x, y = transform.apply(points[0][0], points[0][1])
    _place_run(text, x, y, _font_user(style, frame, doc), "start", "auto", frame, doc)


def _place_run(
    text: str,
    x: float,
    y: float,
    size: float,
    anchor: str,
    baseline: str,
    frame: Frame,
    doc: _Doc,
    width: float | None = None,
) -> None:
    if size <= 0:
        doc.note("a text size could not be read", once=True)
        return
    if width is None:
        width = len(text) * size * 0.55
    if anchor == "middle":
        cx = x
    elif anchor == "end":
        cx = x - width / 2.0
    else:
        cx = x + width / 2.0
    if baseline in {"middle", "central", "mathematical"}:
        cy = y
    elif baseline in {"hanging", "text-before-edge"}:
        cy = y + _BASELINE * size
    else:
        cy = y - _BASELINE * size
    px, py = frame.ctm.apply(cx, cy)
    doc.add_text(text, px, py, width * frame.ctm.scale_x(), size * frame.ctm.scale_y())


def _font_user(style: dict[str, str], frame: Frame, doc: _Doc) -> float:
    size = _user_length(str(style.get("font-size") or "16px"), frame, "y", doc)
    if size is None or size <= 0:
        return 0.0
    return size


def _first_length(element: ET.Element, name: str, frame: Frame, axis: str, doc: _Doc, default: float) -> float:
    raw = _attr(element, name).strip()
    if not raw:
        return default
    first = raw.replace(",", " ").split()[0]
    value = _user_length(first, frame, axis, doc)
    return default if value is None else value


def _collapse(text: str, preserve: bool) -> str:
    if preserve:
        return text.strip("\n")
    return re.sub(r"\s+", " ", text or "").strip()


class _RawChar:
    def __init__(
        self,
        ch: str,
        abs_x: float | None,
        abs_y: float | None,
        dx: float,
        dy: float,
        size: float,
        anchor: str,
        baseline: str,
    ) -> None:
        self.ch = ch
        self.abs_x = abs_x
        self.abs_y = abs_y
        self.dx = dx
        self.dy = dy
        self.size = size
        self.anchor = anchor
        self.baseline = baseline


def _raw_chars(
    node: ET.Element, frame: Frame, style: dict[str, str], doc: _Doc, *, is_root: bool
) -> list[_RawChar]:
    node_style = style if is_root else _compute(node, style, doc)
    found: list[_RawChar] = []
    if _local(node.tag) == "textPath" and not is_root:
        _text_on_path(node, frame, node_style, doc)
        return found
    xs = _length_list(node, "x", frame, "x", doc)
    ys = _length_list(node, "y", frame, "y", doc)
    dxs = _length_list(node, "dx", frame, "x", doc)
    dys = _length_list(node, "dy", frame, "y", doc)
    size = _font_user(node_style, frame, doc)
    anchor = str(node_style.get("text-anchor") or "start")
    baseline = str(node_style.get("dominant-baseline") or "auto")
    if baseline == "auto":
        baseline = str(node_style.get("alignment-baseline") or "auto")
    index = 0

    def push(text: str) -> None:
        nonlocal index
        for ch in text:
            found.append(
                _RawChar(
                    ch,
                    xs[index] if index < len(xs) else None,
                    ys[index] if index < len(ys) else None,
                    dxs[index] if index < len(dxs) else 0.0,
                    dys[index] if index < len(dys) else 0.0,
                    size,
                    anchor,
                    baseline,
                )
            )
            index += 1

    if node.text:
        push(node.text)
    for child in list(node):
        local = _local(child.tag)
        if local in {"tspan", "textPath"}:
            found.extend(_raw_chars(child, frame, node_style, doc, is_root=False))
        if child.tail:
            push(child.tail)
    return found


def _length_list(element: ET.Element, name: str, frame: Frame, axis: str, doc: _Doc) -> list[float]:
    raw = _attr(element, name).strip()
    if not raw:
        return []
    values: list[float] = []
    for part in raw.replace(",", " ").split():
        value = _user_length(part, frame, axis, doc)
        if value is None:
            break
        values.append(value)
    return values


def _char_width(ch: str, size: float) -> float:
    _ = ch
    return max(size, 0.0) * 0.55


def _place_chars(raws: list[_RawChar]) -> list[tuple[str, float, float, float, str]]:
    """Character, left, baseline y, size, baseline name. User space."""

    placed: list[tuple[str, float, float, float, str]] = []
    cursor_x = 0.0
    cursor_y = 0.0
    index = 0
    while index < len(raws):
        if raws[index].abs_x is not None and index + 1 < len(raws) and raws[index + 1].abs_x is not None:
            end = index
            while end < len(raws) and raws[end].abs_x is not None:
                end += 1
            cursor_x, cursor_y = _place_each(raws[index:end], placed, cursor_x, cursor_y)
            index = end
            continue
        end = index + 1
        while end < len(raws) and raws[end].abs_x is None:
            end += 1
        cursor_x, cursor_y = _place_flow(raws[index:end], placed, cursor_x, cursor_y)
        index = end
    return placed


def _place_each(
    raws: list[_RawChar],
    placed: list[tuple[str, float, float, float, str]],
    cursor_x: float,
    cursor_y: float,
) -> tuple[float, float]:
    for raw in raws:
        cursor_y = raw.abs_y if raw.abs_y is not None else cursor_y
        cursor_y += raw.dy
        anchor_x = (raw.abs_x if raw.abs_x is not None else cursor_x) + raw.dx
        width = _char_width(raw.ch, raw.size)
        if raw.anchor == "middle":
            left = anchor_x - width / 2.0
        elif raw.anchor == "end":
            left = anchor_x - width
        else:
            left = anchor_x
        placed.append((raw.ch, left, cursor_y, raw.size, raw.baseline))
        cursor_x = left + width
    return cursor_x, cursor_y


def _place_flow(
    raws: list[_RawChar],
    placed: list[tuple[str, float, float, float, str]],
    cursor_x: float,
    cursor_y: float,
) -> tuple[float, float]:
    if not raws:
        return cursor_x, cursor_y
    first = raws[0]
    cursor_y = first.abs_y if first.abs_y is not None else cursor_y
    cursor_y += first.dy
    anchor_x = (first.abs_x if first.abs_x is not None else cursor_x) + first.dx
    widths = [_char_width(raw.ch, raw.size) for raw in raws]
    total = sum(widths) + sum(raw.dx for raw in raws[1:])
    if first.anchor == "middle":
        left = anchor_x - total / 2.0
    elif first.anchor == "end":
        left = anchor_x - total
    else:
        left = anchor_x
    y = cursor_y
    for raw, width in zip(raws, widths, strict=False):
        if raw is not first:
            left += raw.dx
            y += raw.dy
        placed.append((raw.ch, left, y, raw.size, raw.baseline))
        left += width
    return left, y


def _group_words(placed: list[tuple[str, float, float, float, str]]) -> list[tuple[str, float, float, float, str, str, float]]:
    words: list[tuple[str, float, float, float, str, str, float]] = []
    buf: list[tuple[str, float, float, float, str]] = []

    def flush() -> None:
        if not buf:
            return
        text = "".join(item[0] for item in buf)
        left = buf[0][1]
        size = buf[0][3]
        right = buf[-1][1] + _char_width(buf[-1][0], buf[-1][3])
        width = max(right - left, len(text) * size * 0.55)
        words.append((text, left, buf[0][2], size, "start", buf[0][4], width))
        buf.clear()

    for ch, left, y, size, baseline in placed:
        if ch.isspace():
            flush()
            continue
        if buf:
            prev = buf[-1]
            gap = left - (prev[1] + _char_width(prev[0], prev[3]))
            if abs(y - prev[2]) > 0.45 * max(size, prev[3]) or gap > size * 1.5:
                flush()
        buf.append((ch, left, y, size, baseline))
    flush()
    return words
