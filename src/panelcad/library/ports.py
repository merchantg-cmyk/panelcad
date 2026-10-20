"""Communications ports: library defaults plus instance edits from the object editor."""

from __future__ import annotations

from panelcad.core.models import CommPort, Component, Device, PortKind

KIND_SIDE: dict[str, str] = {
    "ethernet": "right",
    "serial": "left",
    "usb": "bottom",
    "can": "left",
    "fieldbus": "right",
    "other": "right",
}


def parse_port(raw: dict) -> CommPort:
    kind = str(raw.get("kind") or raw.get("type") or "serial").lower()
    if kind in ("eth", "lan", "rj45"):
        kind = "ethernet"
    if kind in ("rs232", "rs-232", "rs485", "rs-485", "com"):
        kind = "serial"
    if kind not in ("ethernet", "serial", "usb", "can", "fieldbus", "other"):
        kind = "other"
    side = raw.get("side") or KIND_SIDE.get(kind, "right")
    return CommPort(
        id=str(raw.get("id") or raw.get("name") or "COM"),
        kind=kind,  # type: ignore[arg-type]
        medium=str(raw.get("medium") or raw.get("media") or ""),
        protocol=str(raw.get("protocol") or ""),
        connector=str(raw.get("connector") or raw.get("plug") or ""),
        side=side if side in ("left", "right", "top", "bottom") else "right",
        label=str(raw.get("label") or ""),
    )


def effective_ports(device: Device, component: Component | None = None) -> list[CommPort]:
    if component is not None and component.ports:
        return list(component.ports)
    return list(device.ports)


def port_matches_medium(port: CommPort, medium: str | None) -> bool:
    if not medium or medium in ("mixed", "other"):
        return True
    m = medium.lower()
    if m == "ethernet":
        return port.kind == "ethernet" or "eth" in port.medium.lower() or "tcp" in port.protocol.lower()
    if m == "serial":
        return port.kind == "serial" or "rs-" in port.medium.lower() or "modbus rtu" in port.protocol.lower()
    if m == "usb":
        return port.kind == "usb"
    return port.kind == m or m in port.medium.lower()


def default_kind_side(kind: PortKind | str) -> str:
    return KIND_SIDE.get(str(kind), "right")


# Shown on a network node when nobody has stored an IP. Not an address.
UNKNOWN_IP_LABEL = "TBD"

# Placeholder words. Compared with spaces removed and case folded.
_UNKNOWN_IP_TOKENS = frozenset({"tbd", "tba", "t.b.d.", "t.b.d"})


def is_known_ip(value: object) -> bool:
    """True when ``value`` is a stored address.

    Blank text and the placeholder TBD are not addresses. A real address,
    including one the user typed, is kept as written.
    """
    text = str(value or "").strip()
    if not text:
        return False
    folded = "".join(text.casefold().split())
    return folded not in _UNKNOWN_IP_TOKENS


def retain_known_ip(properties: dict) -> None:
    """Keep a real IP or an explicit TBD. Drop a blank. Never fill an address in."""
    ip = str(properties.get("ip") or "").strip()
    address = str(properties.get("address") or "").strip()
    if is_known_ip(ip):
        properties["ip"] = ip
        properties["address"] = ip
        return
    if is_known_ip(address):
        properties.pop("ip", None)
        properties["address"] = address
        return
    if ip:
        properties["ip"] = ip
        properties["address"] = ip
        return
    if address:
        properties.pop("ip", None)
        properties["address"] = address
        return
    properties.pop("ip", None)
    properties.pop("address", None)


def _expects_ip(ports: list[CommPort] | None) -> bool:
    if not ports:
        return False
    for port in ports:
        if port.kind == "ethernet":
            return True
        medium = (port.medium or "").lower()
        protocol = (port.protocol or "").lower()
        if "eth" in medium or "tcp" in protocol:
            return True
    return False


def network_address(component: Component) -> str:
    """Display string: IP and/or node/station."""
    props = component.properties
    ip = str(props.get("ip") or props.get("address") or "").strip()
    node = str(props.get("node") or props.get("station") or "").strip()
    if ip and node:
        return f"{ip}   node {node}"
    if ip:
        return ip
    if node:
        return f"node {node}"
    return ""


def network_node_caption(component: Component, ports: list[CommPort] | None = None) -> str:
    """Text drawn on a network node.

    A known IP is shown as stored. An Ethernet device with no known IP shows
    TBD. A serial-only device with a node keeps that node and does not gain
    an IP label. This does not invent an address.
    """
    props = component.properties or {}
    raw = str(props.get("ip") or props.get("address") or "").strip()
    node = str(props.get("node") or props.get("station") or "").strip()
    if is_known_ip(raw):
        ip = raw
    elif _expects_ip(ports) or not node:
        ip = UNKNOWN_IP_LABEL
    else:
        ip = ""
    if ip and node:
        return f"{ip}   node {node}"
    if ip:
        return ip
    if node:
        return f"node {node}"
    return UNKNOWN_IP_LABEL
