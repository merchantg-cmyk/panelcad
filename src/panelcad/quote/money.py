"""Money helpers. No catalog prices live here.

Arithmetic stays in Decimal and rounds half up. A float is only produced when a
YAML field or a spreadsheet number needs one, and that float is built from the
already-rounded decimal text.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP

_CENTS = Decimal("0.01")
_HOURS = Decimal("0.001")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


class Money:
    """A cent-rounded amount the xlsx writer stores as a number, not text."""

    def __init__(self, amount: Decimal | float | int | str) -> None:
        self.amount = cents(amount)

    def text(self) -> str:
        return format(self.amount, ".2f")


def to_decimal(value: object) -> Decimal:
    """Finite Decimal. Floats go through their shortest round-trip, not binary math."""

    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, bool) or value is None:
        raise ValueError("not a finite number")
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("not a finite number")
        number = Decimal(str(value))
    else:
        parsed = parse_number(value)
        if parsed is None:
            raise ValueError("not a finite number")
        number = parsed
    if not number.is_finite():
        raise ValueError("not a finite number")
    return number


def cents(value: Decimal | float | str | int) -> Decimal:
    """Round half up to a cent."""

    return to_decimal(value).quantize(_CENTS, rounding=ROUND_HALF_UP)


def money_num(value: Decimal | float | str | int) -> float:
    """Float whose default text is the cent-rounded amount. For YAML fields only."""

    return float(format(cents(value), ".2f"))


def hours_from_minutes(minutes: Decimal | float | str | int) -> Decimal:
    """Display hours: minutes / 60, half up to 3 decimal places. Not used to price a line."""

    return (to_decimal(minutes) / Decimal(60)).quantize(_HOURS, rounding=ROUND_HALF_UP)


def parse_number(value: object) -> Decimal | None:
    """Parse a user-typed cost. Blank is missing. Does not invent a number.

    A comma is the decimal mark when it is the last separator and one or two
    digits follow (``12,50``, ``1.234,50``). ``1,234`` and ``1.234`` are
    rejected: they can be thousands or a decimal. A leading or trailing ISO
    currency code is accepted (``USD 12.50``, ``12.50 USD``). NaN and infinity
    are rejected.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("not a finite number")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("not a finite number")
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    for symbol in ("$", "€", "£"):
        text = text.replace(symbol, "")
    text = text.strip()
    parts = text.split()
    if len(parts) == 2 and _CURRENCY.match(parts[1]):
        text = parts[0]
    elif len(parts) == 2 and _CURRENCY.match(parts[0]):
        text = parts[1]
    elif len(parts) > 1:
        raise ValueError(f"ambiguous number {value!r}")
    text = text.replace(" ", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    negative = text.startswith("-")
    body = text[1:] if negative else text
    if not body or body.lower() in {"nan", "inf", "infinity", "+inf", "-inf"}:
        raise ValueError("not a finite number")
    if not re.fullmatch(r"[0-9.,]+", body):
        raise ValueError(f"not a number: {value!r}")
    normalized = _normalize_separators(body)
    number = Decimal(normalized)
    if negative:
        number = -number
    if not number.is_finite():
        raise ValueError("not a finite number")
    return number


def _normalize_separators(body: str) -> str:
    if "." in body and "," in body:
        if body.rfind(",") > body.rfind("."):
            return body.replace(".", "").replace(",", ".")
        return body.replace(",", "")
    sep = "," if "," in body else "." if "." in body else ""
    if not sep:
        return body
    chunks = body.split(sep)
    # One separator and exactly three digits after it can be thousands or a decimal.
    if len(chunks) == 2 and len(chunks[1]) == 3 and chunks[0] != "":
        raise ValueError(
            f"ambiguous number {body!r}. Use 12.50 or 12,50. A group of three digits needs both separators (1,234.50)."
        )
    if sep == ",":
        if len(chunks) == 2 and len(chunks[1]) in (1, 2) and chunks[0] != "":
            return chunks[0] + "." + chunks[1]
        if len(chunks) > 2 and all(len(chunk) == 3 for chunk in chunks[1:]) and 1 <= len(chunks[0]) <= 3:
            return "".join(chunks)
        raise ValueError(
            f"ambiguous number {body!r}. Use 12.50 or 12,50. A group of three digits needs both separators (1,234.50)."
        )
    if len(chunks) > 2:
        if all(len(chunk) == 3 for chunk in chunks[1:]) and 1 <= len(chunks[0]) <= 3:
            return "".join(chunks)
        raise ValueError(f"ambiguous number {body!r}")
    return body


def format_percent(value: Decimal | float | str | int) -> str:
    """A percent for a label. 10 stays 10, 12.50 is 12.5, and 0 is 0."""

    return format(to_decimal(value).normalize(), "f")


def format_amount(value: Decimal | float | int | None, currency: str = "") -> str:
    if value is None or value == "":
        return ""
    text = f"{cents(value):,.2f}"
    code = str(currency or "").strip()
    if code:
        return f"{text} {code}"
    return text


def format_unit_price(extended_sell: Decimal, qty: int) -> str:
    """Display-only unit price. The quote total uses extended sell, not this figure."""

    if qty <= 0:
        return ""
    unit = (cents(extended_sell) / Decimal(qty)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = format(unit, "f")
    if "." in text:
        whole, frac = text.split(".")
        frac = frac.rstrip("0")
        if len(frac) < 2:
            frac = frac.ljust(2, "0")
        text = whole + "." + frac
    return text
