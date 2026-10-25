"""Pick a library symbol from a printed type word or the tag prefix.

Catalog numbers are not guessed here. An unknown symbol stays unresolved.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.library.catalog import Catalog, normalize_standard

# A prefix with no drawn shape is a guess. It must not look as sure as a matched symbol.
PREFIX_CONFIDENCE = 0.48

TYPE_QUERY = {
    "breaker": "breaker",
    "mcb": "breaker",
    "mccb": "breaker",
    "mpcb": "mpcb",
    "motor-protector": "mpcb",
    "motor protector": "mpcb",
    "contactor": "contactor",
    "overload": "overload",
    "motor": "motor",
    "estop": "estop",
    "e-stop": "estop",
    "pushbutton": "pushbutton",
    "pilot": "pilot",
    "plc": "plc",
    "hmi": "hmi",
    "power_supply": "power supply",
    "psu": "power supply",
    "drive": "vfd",
    "vfd": "vfd",
    "terminal": "terminal",
    "terminal block": "terminal",
    "relay": "relay",
    "coil": "coil",
    "contact": "contact",
    "fuse": "fuse",
    "disconnect": "disconnect",
    "float": "float",
    "level": "level",
    "pressure": "pressure",
    "temperature": "temperature",
    "temp": "temperature",
    "flow": "flow",
    "foot": "foot",
    "horn": "horn",
    "buzzer": "horn",
    "alternating": "alternating",
    "feed-through": "terminal",
    "feedthrough": "terminal",
}

# Search words are not the class names the accuracy table uses.
_CLASS_OF_QUERY = {
    "breaker": "breaker",
    "mpcb": "mpcb",
    "contactor": "contactor",
    "overload": "overload",
    "motor": "motor",
    "estop": "estop",
    "pushbutton": "pushbutton",
    "pilot": "pilot",
    "plc": "plc",
    "hmi": "hmi",
    "power supply": "power_supply",
    "vfd": "drive",
    "terminal": "terminal",
    "relay": "relay",
    "coil": "coil",
    "contact": "contact",
    "fuse": "fuse",
    "disconnect": "disconnect",
    "float": "float",
    "level": "level",
    "pressure": "pressure",
    "temperature": "temperature",
    "flow": "flow",
    "foot": "foot",
    "horn": "horn",
    "alternating": "alternating",
}


def shape_class(device: Any) -> str:
    """Drawn-symbol family. A plain breaker and a motor protector are different classes."""

    lib = str(getattr(device, "library_id", "") or "").lower()
    cat = str(getattr(device, "category", "") or "").lower()
    blob = f"{lib} {cat}"
    leaf = lib.rsplit(".", 1)[-1]
    if "mpcb" in blob:
        return "mpcb"
    if "overload" in blob or leaf.startswith("ol_"):
        return "overload"
    if any(token in blob for token in ("breaker", "mcb", "mccb")):
        return "breaker"
    if leaf == "coil" or leaf.endswith("_coil"):
        return "coil"
    if "contactor" in blob:
        return "contactor"
    if "motor" in blob:
        return "motor"
    if "terminal" in blob:
        return "terminal"
    if "float" in blob:
        return "float"
    if "level" in blob:
        return "level"
    if "pressure" in blob:
        return "pressure"
    if "temperature" in blob or "temp" in leaf:
        return "temperature"
    if "flow" in blob:
        return "flow"
    if "foot" in blob:
        return "foot"
    if "horn" in blob or "buzzer" in blob:
        return "horn"
    if "alternating" in blob:
        return "alternating"
    if cat.startswith("timing.contact") or leaf.startswith("contact_"):
        return "timer_contact"
    if "estop" in blob or "e-stop" in blob:
        return "estop"
    if "pushbutton" in blob or ".pb_" in lib or leaf.startswith("pb_"):
        return "pushbutton"
    if "pilot" in blob or cat.startswith("operator.light") or "light" in leaf:
        return "pilot"
    if "hmi" in blob:
        return "hmi"
    if cat.startswith("control.plc") or "plc" in blob or ".click." in lib or leaf.startswith("click"):
        return "plc"
    if "psu" in lib or "supply" in blob:
        return "power_supply"
    if cat.startswith("drive") or "vfd" in blob or "durapulse" in lib:
        return "drive"
    if "relay" in blob:
        return "relay"
    if "fuse" in blob:
        return "fuse"
    if "disconnect" in blob:
        return "disconnect"
    if "contact" in blob:
        return "contact"
    return cat.rsplit(".", 1)[-1] if cat else ""


# Printed families a schematic can build. A1, R1, 10C, and 12T are not in this set.
# Free-form NEMA names (DRIVE, MOT, PWS, PLT) alias to the catalog prefix below.
SUPPORTED_TAG_FAMILIES = {
    "OL": "overload",
    "M": "contactor",
    "CR": "relay",
    "FU": "fuse",
    "CB": "breaker",
    "PB": "pushbutton",
    "PBLT": "pushbutton",
    "LT": "pilot",
    "PL": "pilot",
    "PLT": "pilot",
    "MS": "contactor",
    "DS": "disconnect",
    "DISC": "disconnect",
    "TR": "timer",
    "K": "contactor",
    "Q": "breaker",
    "F": "fuse",
    "CON": "contactor",
    "G": "motor",
    "DRIVE": "drive",
    "VFD": "drive",
    "MOT": "motor",
    "MTR": "motor",
    "PWS": "power_supply",
    "PS": "power_supply",
    "HMI": "hmi",
    "S": "pushbutton",
    "LS": "limit",
    "TB": "terminal",
    "PLC": "plc",
    "FS": "float",
    "LEV": "level",
    "PRS": "pressure",
    "TSW": "temperature",
    "FSW": "flow",
    "FTS": "foot",
    "HN": "horn",
    "AR": "alternating",
}

# Printed NEMA letters that do not match the library ref_prefix.
_CATALOG_PREFIX = {
    "DRIVE": "VFD",
    "MOT": "MTR",
    "PWS": "PS",
    "PLT": "PL",
    "PBLT": "PB",
}

def supported_tag_family(tag: str) -> str:
    """The device family this printed tag names, or empty when the sheet cannot build it."""

    from panelcad.importer.read import is_device_tag

    text = str(tag or "").strip().upper()
    if not text or not is_device_tag(text):
        return ""
    if re.fullmatch(r"\d+G", text):
        return "motor"
    return SUPPORTED_TAG_FAMILIES.get(tag_prefix(text), "")


# A drawn class, reduced to the family the tag letters name. Coil and contact
# are parts of a device, not a second family, so they do not contradict the tag.
_SYMBOL_FAMILY = {
    "breaker": "breaker",
    "mpcb": "breaker",
    "contactor": "contactor",
    "starter": "contactor",
    "overload": "overload",
    "motor": "motor",
    "relay": "relay",
    "fuse": "fuse",
    "disconnect": "disconnect",
    "pushbutton": "pushbutton",
    "estop": "pushbutton",
    "pilot": "pilot",
}


def symbol_class_family(symbol: str) -> str:
    """The device family a part-type word names, or empty when it is not one."""

    text = str(symbol or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _SYMBOL_FAMILY.get(text, "")


# Vision rows say "contact", "pilot_light", or "push_button". The catalog says
# "contactor", "pilot", or "pushbutton". Occurrence comparison uses one set of words.
_SHAPE_WORD = {
    "push_button": "pushbutton",
    "pushbutton": "pushbutton",
    "pb": "pushbutton",
    "pilot_light": "pilot",
    "pilot": "pilot",
    "indicator": "pilot",
    "light": "pilot",
    "aux": "contact",
    "auxiliary": "contact",
    "contact": "contact",
    "coil": "coil",
    "contactor": "contactor",
    "starter": "contactor",
    "overload": "overload",
    "motor": "motor",
    "relay": "relay",
    "fuse": "fuse",
    "disconnect": "disconnect",
    "breaker": "breaker",
    "mpcb": "mpcb",
    "estop": "pushbutton",
    "e_stop": "pushbutton",
}


def normalize_symbol_word(symbol: str) -> str:
    """One name for a drawn class, whether it came from the model or the catalog."""

    text = str(symbol or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not text:
        return ""
    if text in _SHAPE_WORD:
        return _SHAPE_WORD[text]
    family = symbol_class_family(text)
    return family or text


def tag_symbol_mismatch(device: dict[str, Any]) -> bool:
    """True when a number-first tag's family and the drawn part type disagree.

    10M is a contactor, not a motor. Letter-first tags keep the drawn class:
    M1 is a motor and F1 is an overload. Prefix F drawn as an overload is the
    IEC thermal overload, whose catalog ref is F, not a fuse misread. An empty
    part type does not contradict the tag.
    """

    tag = str(device.get("tag") or "").strip()
    if not tag or not tag[0].isdigit():
        return False
    family = supported_tag_family(tag)
    asked = symbol_class_family(str(device.get("symbol") or device.get("type") or ""))
    if not family or not asked or asked == family:
        return False
    if tag_prefix(tag) == "F" and asked == "overload":
        return False
    return True


def print_tag_placeable(device: dict[str, Any]) -> bool:
    """Place a supported tag, or ink that named a symbol. Never a generic stand-in.

    A1, R1, 12T, and 10C are not families this sheet builds. M6 with no
    confirmed symbol is not aux_nc. Q1, K1, F1, and 10M-G still resolve.
    """

    if device.get("unconfirmed") or device.get("title_ink") or device.get("tag_unparsed") or device.get("unlocalised"):
        return False
    if tag_symbol_mismatch(device):
        return False
    family = supported_tag_family(str(device.get("tag") or ""))
    library = str(device.get("library_id") or "")
    source = str(device.get("source") or "")
    # Ink or a vector stroke named this symbol. The tag can be A2 or U1.
    if library and source in {"symbol", "vector"}:
        return True
    if not family:
        return False
    # A missed template used to keep a prefix library on an OCR token.
    # A supported tag still places. F1 read as an overload is that part.
    if source == "ocr":
        return False
    return True


def tag_prefix(tag: str) -> str:
    """Letters that pick a library family. Number-first tags use the letters after the digits.

    10M-G is M, 10OL-G is OL, 2CR is CR. K1 stays K. FU1-7 and S-204 stay FU and S.
    A PLC address such as Q40.2L is not the breaker prefix Q.
    """

    from panelcad.importer.read import iec_product_tag, is_plc_address, normalize_device_tag

    # -K1 and =A1-K1 are the product K. The aspect mark is not a prefix.
    text = iec_product_tag(tag) or normalize_device_tag(tag)
    if is_plc_address(text):
        return ""
    if text in {"PWS", "HMI"}:
        return text
    head = text.split("-", 1)[0]
    number_first = re.match(r"\d+([A-Z]+)", head)
    if number_first:
        return number_first.group(1)
    letters = []
    for ch in head:
        if ch.isalpha():
            letters.append(ch)
        else:
            break
    return "".join(letters)


def catalog_prefix(tag: str) -> str:
    """Library ref_prefix for a printed tag. DRIVE is VFD, MOT is MTR, PLT is PL."""

    prefix = tag_prefix(tag)
    return _CATALOG_PREFIX.get(prefix, prefix)


def _nema_devices(devices: list[Any]) -> list[Any]:
    """Library rows whose id is ``nema.*``. An IEC symbol is not a NEMA match."""

    return [device for device in devices if str(getattr(device, "library_id", "") or "").startswith("nema.")]


_GROUND_TAGS = {"PE", "GND", "GRD", "EG", "GROUND", "EARTH"}


def tag_is_ground(tag: str) -> bool:
    """True for a protective-earth tag. TB1 is not a ground terminal."""

    text = str(tag or "").strip().upper()
    if text in _GROUND_TAGS:
        return True
    return tag_prefix(text) in _GROUND_TAGS


def is_ground_symbol(device: Any) -> bool:
    """A PE / ground terminal. A feed-through terminal is not one."""

    lib = str(getattr(device, "library_id", "") or "").lower()
    cat = str(getattr(device, "category", "") or "").lower()
    props = getattr(device, "properties", {}) or {}
    kind = str(props.get("type") or "").lower()
    if kind in {"pe", "ground", "earth"}:
        return True
    if "terminals.pe" in lib or lib.endswith(".pe") or ".pe_" in lib:
        return True
    return "ground" in cat or cat.endswith(".pe")


def _drop_ground(devices: list[Any], tag: str) -> list[Any]:
    if tag_is_ground(tag):
        return devices
    return [device for device in devices if not is_ground_symbol(device)]


def _symbol_only(device: Any) -> bool:
    panel = getattr(device, "panel", None)
    if panel is None:
        return False
    return float(getattr(panel, "width_mm", 0) or 0) == 0 and float(getattr(panel, "height_mm", 0) or 0) == 0


def _classes_agree(asked: str, got: str) -> bool:
    """A coil or a contact is part of the device the tag names, not a different class."""

    if not asked or not got or asked == got:
        return True
    if asked in {"contact", "coil"} and got in {"relay", "contactor", "coil", "contact", "timer"}:
        return True
    if got in {"contact", "coil"} and asked in {"relay", "contactor", "timer"}:
        return True
    return False


def _specific_rating(device: Any) -> bool:
    """A published coil or contact arrangement. Not a substring of the library id."""

    props = getattr(device, "properties", {}) or {}
    return bool(str(props.get("coil") or "").strip() or str(props.get("contacts") or "").strip())


def generic_family_symbol(
    catalog: Catalog,
    *,
    tag: str,
    library_id: str = "",
    type_text: str = "",
    standard: str = "",
) -> str:
    """A same-family symbol that has pins and no catalog size.

    Used when the resolved part contradicts the print. Empty when the family
    itself is unknown.
    """

    std = normalize_standard(standard, default="") if standard else ""
    part = catalog.devices.get(library_id) if library_id else None
    asked = normalize_symbol_word(type_text)
    family = ""
    if asked and asked not in {"contact", "coil", "wire"}:
        family = asked
    elif part is not None:
        family = shape_class(part)
    if not family:
        family = supported_tag_family(tag)
    if not family:
        return ""
    polarity = ""
    asked_text = str(type_text or "").lower().replace("-", "_")
    if asked_text.endswith("_nc") or asked_text in {"nc", "ncto", "nctc"}:
        polarity = "nc"
    elif asked_text.endswith("_no") or asked_text in {"no", "notc", "noto"}:
        polarity = "no"
    found = []
    for device in catalog.devices.values():
        lib = str(device.library_id or "")
        if std == "NEMA/ANSI" and not lib.startswith("nema."):
            continue
        if std == "IEC" and not lib.startswith("iec."):
            continue
        if shape_class(device) != family:
            continue
        if not getattr(device, "pins", None):
            continue
        if is_ground_symbol(device) and not tag_is_ground(tag):
            continue
        if "offpage" in lib or "notes" in lib:
            continue
        found.append(device)
    if polarity:
        sided = [device for device in found if polarity in str(device.library_id).lower()]
        if sided:
            found = sided
    plain = [device for device in found if _symbol_only(device)]
    if not plain:
        return ""
    return _prefer(plain).library_id


def resolve_symbol(
    catalog: Catalog,
    *,
    tag: str,
    type_text: str = "",
    standard: str = "",
    library_id: str = "",
) -> dict[str, Any]:
    """Return library_id, confidence, and whether the symbol was only a prefix guess.

    On a NEMA/ANSI print the result is a ``nema.*`` row, or empty when the
    catalog has no NEMA part for that tag. An IEC id is not kept.
    """

    std = normalize_standard(standard, default="") if standard else ""
    nema = std == "NEMA/ANSI"
    explicit = str(library_id or "").strip()
    if explicit and explicit in catalog.devices and (
        not nema or explicit.startswith("nema.") or explicit.startswith("iec.connection.offpage")
    ):
        device = catalog.devices[explicit]
        return {"library_id": explicit, "confidence": 0.95, "guess": False, "symbol": shape_class(device)}
    prefix = catalog_prefix(tag)
    asked = type_text.strip().lower()
    # "wire" searches the wire duct. "contact" searches a 2-pin contact or a coil.
    # A power tag keeps the power symbol.
    if asked in {"wire", "duct", "line", "conductor"}:
        asked = ""
    if asked in {"contact", "coil"} and prefix in {"M", "MS", "K", "CON"}:
        asked = "contactor"
    elif asked in {"contact", "coil", "wire"} and prefix == "OL":
        asked = "overload"
    if re.fullmatch(r"\d+G", str(tag or "").upper()) and asked in {"", "wire", "contact", "coil"}:
        asked = "motor"
    query = TYPE_QUERY.get(asked, asked)
    wanted = _CLASS_OF_QUERY.get(query, "")
    if query:
        hits = _usable(catalog.search(query, standard=std or None))
        if nema:
            hits = _nema_devices(hits)
        elif not hits and std:
            hits = _usable(catalog.search(query))
        hits = _drop_ground(hits, tag)
        if wanted:
            typed = [device for device in hits if shape_class(device) == wanted]
            if typed:
                hits = typed
        if wanted in {"contactor", "overload", "motor"}:
            # A 2-pin aux contact or a 1-phase motor is not the power symbol.
            rich = [
                device
                for device in hits
                if len(device.pins) >= 3
                and "aux" not in device.library_id.lower()
                and "ol_contact" not in device.library_id.lower()
                and "1ph" not in device.library_id.lower()
            ]
            if rich:
                hits = rich
        if len(hits) == 1:
            return {"library_id": hits[0].library_id, "confidence": 0.84, "guess": False, "symbol": shape_class(hits[0])}
        if hits:
            prefix = catalog_prefix(tag)
            pref = [device for device in hits if device.ref_prefix.upper() == prefix]
            chosen = _choose_symbol(pref or hits, asked)
            # The crop named a class, not a catalog row. More than one row is still a guess.
            sure = len(pref or hits) == 1
            return {
                "library_id": chosen.library_id,
                "confidence": 0.84 if sure else 0.55,
                "guess": not sure,
                "symbol": shape_class(chosen),
            }
    if asked in {"float", "level", "pressure", "temperature", "flow", "foot", "horn", "alternating"}:
        # A float switch is not a limit switch. No symbol of that class stays unresolved.
        return {"library_id": "", "confidence": 0.0, "guess": False, "symbol": ""}
    prefix = catalog_prefix(tag)
    if not prefix and tag_prefix(tag) != "HMI":
        return {"library_id": "", "confidence": 0.0, "guess": False, "symbol": ""}
    pool = [
        device
        for device in catalog.devices.values()
        if prefix and device.ref_prefix.upper() == prefix and (not std or device.standard == std)
    ]
    pool = _usable(pool)
    if nema:
        pool = _nema_devices(pool)
    elif not pool and prefix:
        pool = _usable([device for device in catalog.devices.values() if device.ref_prefix.upper() == prefix])
    if not pool and nema:
        # The printed family still names a NEMA symbol when the ref letters differ
        # (K is a contactor, S is a pushbutton). No NEMA row stays unresolved.
        family = supported_tag_family(tag)
        family_query = TYPE_QUERY.get(family, "")
        if family_query:
            pool = _nema_devices(_usable(catalog.search(family_query, standard=std)))
            wanted_family = _CLASS_OF_QUERY.get(family_query, "")
            if wanted_family:
                typed = [device for device in pool if shape_class(device) == wanted_family]
                if typed:
                    pool = typed
    if not pool and tag_prefix(tag) == "HMI" and not nema:
        pool = _usable(
            [
                device
                for device in catalog.devices.values()
                if shape_class(device) == "hmi" or "hmi" in device.library_id.lower()
            ]
        )
    pool = _drop_ground(pool, tag)
    pool = _pushbutton_over_limit(pool, tag, asked)
    if not pool:
        return {"library_id": "", "confidence": 0.0, "guess": False, "symbol": ""}
    upper = str(tag or "").upper()
    if re.fullmatch(r"\d+M(?:-[A-Z0-9]+)?", upper):
        contactors = [
            device
            for device in pool
            if "contactor" in device.library_id.lower()
            and len(device.pins) >= 3
            and "aux" not in device.library_id.lower()
            and "coil" not in device.library_id.lower()
        ]
        if contactors:
            pool = contactors
    elif re.fullmatch(r"\d+G", upper):
        motors = [
            device
            for device in catalog.devices.values()
            if shape_class(device) == "motor" and "1ph" not in device.library_id.lower() and len(device.pins) >= 3
        ]
        if nema:
            motors = _nema_devices(motors)
        if motors:
            pool = motors
    elif re.fullmatch(r"\d+OL(?:-[A-Z0-9]+)?", upper):
        overloads = [
            device
            for device in pool
            if shape_class(device) == "overload" and "ol_contact" not in device.library_id.lower() and len(device.pins) >= 3
        ]
        if overloads:
            pool = overloads
    chosen = _choose_symbol(pool, asked)
    # The letters in front of the number are not the symbol that was drawn.
    return {
        "library_id": chosen.library_id,
        "confidence": PREFIX_CONFIDENCE,
        "guess": True,
        "symbol": shape_class(chosen),
    }


def _usable(devices: list[Any]) -> list[Any]:
    out = []
    for device in devices:
        lib = device.library_id.lower()
        if "offpage" in lib or "notes" in lib:
            continue
        out.append(device)
    return out


def _contact_sense(text: str) -> str:
    """NO or NC when the print named the contact. An empty tag has neither."""

    asked = str(text or "").lower().replace("-", "_").strip()
    if asked.endswith("_nc") or asked in {"nc", "ncto", "nctc"}:
        return "nc"
    if asked.endswith("_no") or asked in {"no", "notc", "noto"}:
        return "no"
    return ""


def _pushbutton_over_limit(devices: list[Any], tag: str, asked: str) -> list[Any]:
    """An IEC S tag with no contact sense is a pushbutton, not a limit switch.

    Limit switches share the S prefix and have no panel size, so a prefix
    guess would otherwise claim the NC limit. A named NO or NC contact still
    follows that sense.
    """

    if supported_tag_family(tag) != "pushbutton" or _contact_sense(asked):
        return devices
    if not any(shape_class(device) == "limit" for device in devices):
        return devices
    plain = [device for device in devices if str(device.library_id).rsplit(".", 1)[-1] == "pb_no"]
    return plain or devices


def _asked_rating(text: str) -> bool:
    """True when the print itself stated a voltage. Library ids are not consulted."""

    from panelcad.core.checks import parse_volts

    return parse_volts(text) is not None


def _choose_symbol(devices: list[Any], asked: str) -> Any:
    """A symbol with no catalog size wins when the print did not state a voltage."""

    if not _asked_rating(asked):
        plain = [device for device in devices if _symbol_only(device)]
        if plain:
            devices = plain
    return _prefer(devices)


def _prefer(devices: list[Any]) -> Any:
    """Stable choice. A 3-pole power symbol beats a coil, an aux contact, or a 1-phase motor."""

    def weak(device: Any) -> bool:
        lib = device.library_id.lower()
        return "1ph" in lib or "aux" in lib or "duct" in lib or lib.endswith("coil") or ".coil" in lib

    rich = [device for device in devices if len(device.pins) >= 3 and not weak(device)]
    pool = rich or devices

    def key(device: Any) -> tuple:
        lib = device.library_id.lower()
        return ("mpcb" in lib, "aux" in lib, weak(device), len(device.pins), device.library_id)

    return sorted(pool, key=key)[0]


def symbol_rank(device: dict[str, Any]) -> int:
    """A later contactor, overload, or motor beats the first tile's coil or contact."""

    symbol = str(device.get("symbol") or device.get("type") or "").lower().replace(" ", "_")
    lib = str(device.get("library_id") or "").lower()
    if "1ph" in lib or "duct" in lib or symbol in {"wire", "duct"}:
        return 0
    if symbol in {"contact", "coil"} or "aux" in lib or lib.endswith("coil") or ".coil" in lib:
        return 1
    if symbol in {"contactor", "overload", "motor", "breaker", "mpcb"} or any(
        token in lib for token in ("contactor", "overload", "motor_3", "breaker", "mpcb")
    ):
        return 3
    if symbol:
        return 2
    return 1
