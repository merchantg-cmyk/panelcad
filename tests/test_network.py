import re
from pathlib import Path

from panelcad import Session
from panelcad.examples.dol import build_dol
from panelcad.library.ports import is_known_ip, network_node_caption
from panelcad.render.svg import sheet_to_svg

_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _stored_ip(comp) -> str:
    return str(comp.properties.get("ip") or comp.properties.get("address") or "").strip()


def _unknown_ip(value: object) -> bool:
    text = str(value or "").strip()
    return text == "" or text.upper() == "TBD"


def test_library_ports_on_plc_and_hmi():
    s = Session.create("Ports")
    cpu = s.inspect_device("adc.click.c2_03cpu")
    ids = {p["id"] for p in cpu["ports"]}
    assert {"ETH", "COM1", "COM2", "USB"} <= ids
    eth = next(p for p in cpu["ports"] if p["id"] == "ETH")
    assert eth["kind"] == "ethernet"
    hmi = s.inspect_device("iec.hmi.panel_7in")
    assert any(p["id"] == "ETH" for p in hmi["ports"])


def test_object_editor_add_port_roundtrip(tmp_path: Path):
    s = Session.create("Edit")
    s.place("adc.click.c2_03cpu", ref="A2")
    assert any(p["id"] == "ETH" for p in s.list_ports("A2"))
    s.add_port("A2", "COM3", kind="serial", medium="RS-485", protocol="Modbus RTU")
    ids = {p["id"] for p in s.list_ports("A2")}
    assert "COM3" in ids
    s.set_port("A2", "COM3", protocol="DF1")
    assert next(p for p in s.list_ports("A2") if p["id"] == "COM3")["protocol"] == "DF1"
    s.delete_port("A2", "COM3")
    assert "COM3" not in {p["id"] for p in s.list_ports("A2")}
    path = tmp_path / "p.pcad.yaml"
    s.save(path)
    again = Session.open(path)
    assert {p["id"] for p in again.list_ports("A2")} >= {"ETH", "COM1", "COM2"}


def test_network_addresses_assigned_and_editable():
    s = build_dol()
    eth = s.sheet("ethernet")
    a2 = eth.component("A2")
    assert _unknown_ip(_stored_ip(a2))
    assert not _IPV4.match(_stored_ip(a2))
    p1 = eth.component("P1")
    assert _unknown_ip(_stored_ip(p1))
    assert _unknown_ip(_stored_ip(s.sheet("io").component("A2")))
    s.set_address("A2", ip="10.0.0.5", node="12")
    assert s.sheet("ethernet").component("A2").properties["ip"] == "10.0.0.5"
    assert s.sheet("io").component("A2").properties["ip"] == "10.0.0.5"
    ser = s.sheet("serial")
    assert str(ser.component("U1").properties.get("node") or ser.component("U1").properties.get("station") or "")
    assert _unknown_ip(_stored_ip(ser.component("U1")))


def test_layout_network_does_not_invent_ip_addresses():
    """Unknown IPs stay blank or TBD on any project. Known addresses are kept."""
    s = Session.create("Comms")
    s.place("iec.network.switch_5p", ref="SW1", x=40, y=40)
    s.place("iec.hmi.panel_7in", ref="HMI1", x=140, y=40)
    s.place("iec.plc.compact_16dio", ref="PLC1", x=240, y=40)
    s.place("iec.network.switch_5p", ref="SW2", x=40, y=120)
    s.place("adc.click.c2_03cpu", ref="CPU1", x=140, y=120)
    s.set_component_properties("SW1", {"address": "172.16.8.8"})
    s.set_address("HMI1", ip="10.1.2.3")
    s.set_address("PLC1", ip="192.168.1.10")
    s.set_component_properties("SW2", {"ip": "TBD"})
    s.set_component_properties("CPU1", {"ip": "   "})
    s.add_sheet("lan", title="LAN", kind="network")
    s.set_network(name="LAN", medium="ethernet", protocol="Ethernet")
    first = s.layout_network(sheet_id="lan", medium="ethernet")
    assert first["devices"] == 5
    again = s.layout_network(sheet_id="lan", medium="ethernet")
    assert again["devices"] == 5

    known = {
        "SW1": "172.16.8.8",
        "HMI1": "10.1.2.3",
        "PLC1": "192.168.1.10",
    }
    for sheet_id in ("main", "lan"):
        sheet = s.sheet(sheet_id)
        for ref, ip in known.items():
            assert _stored_ip(sheet.component(ref)) == ip
        for ref in ("SW2", "CPU1"):
            stored = _stored_ip(sheet.component(ref))
            assert _unknown_ip(stored), stored
            assert not _IPV4.match(stored)

    lan = s.sheet("lan")
    for comp in lan.components:
        stored = _stored_ip(comp)
        assert stored in {"", "TBD", "172.16.8.8", "10.1.2.3", "192.168.1.10"}
        assert stored != "192.168.1.11"
        assert stored != "192.168.1.12"

    svg = sheet_to_svg(lan, s.catalog, s.project)
    assert "172.16.8.8" in svg
    assert "10.1.2.3" in svg
    assert "192.168.1.10" in svg
    assert "TBD" in svg
    assert "192.168.1.11" not in svg
    assert "192.168.1.12" not in svg
    assert "(no address)" not in svg

    blank = lan.component("CPU1")
    device = s.catalog.get(blank.library_id)
    from panelcad.library.ports import effective_ports

    caption = network_node_caption(blank, effective_ports(device, blank))
    assert caption == "TBD" or caption.startswith("TBD")
    assert is_known_ip("") is False
    assert is_known_ip("TBD") is False
    assert is_known_ip("tbd") is False
    assert is_known_ip("10.1.2.3") is True


def test_network_pages_from_dol():
    s = build_dol()
    ids = [sh.id for sh in s.project.sheets]
    assert "ethernet" in ids
    assert "serial" in ids
    eth = s.sheet("ethernet")
    assert eth.kind == "network"
    assert eth.network is not None
    assert eth.network.medium == "ethernet"
    refs = {c.id for c in eth.components}
    assert "A2" in refs
    assert "P1" in refs
    assert any(w.net == "LAN" for w in eth.wires)
    ser = s.sheet("serial")
    assert {c.id for c in ser.components} >= {"A2", "U1"}
    assert s.validate() == []


def test_agent_layout_network():
    from panelcad.cli.agent import Agent

    agent = Agent()
    agent.handle({"id": 1, "op": "create_project", "name": "N"})
    agent.handle({"id": 2, "op": "place", "library_id": "adc.click.c2_03cpu", "ref": "A2", "x": 40, "y": 40})
    agent.handle({"id": 3, "op": "place", "library_id": "iec.hmi.panel_7in", "ref": "P1", "x": 140, "y": 40})
    r = agent.handle({"id": 4, "op": "add_sheet", "sheet_id": "lan", "title": "LAN", "kind": "network", "medium": "ethernet"})
    assert r["ok"]
    r = agent.handle({"id": 5, "op": "layout_network", "sheet": "lan", "medium": "ethernet"})
    assert r["ok"]
    assert r["result"]["devices"] >= 2
    for sheet_id in ("main", "lan"):
        for comp in agent.session.sheet(sheet_id).components:
            stored = _stored_ip(comp)
            assert _unknown_ip(stored), stored
            assert not _IPV4.match(stored)
    r = agent.handle({"id": 6, "op": "list_ports", "ref": "A2"})
    assert r["ok"]
    assert any(p["id"] == "ETH" for p in r["result"]["ports"])
