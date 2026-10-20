from panelcad.library.catalog import default_catalog


def test_builtin_library_loads():
    cat = default_catalog()
    assert len(cat.devices) >= 20
    assert "iec.breakers.mcb_3p_32a" in cat.devices
    assert "iec.plc.compact_16dio" in cat.devices
    assert "iec.hmi.panel_7in" in cat.devices
    q = cat.get("iec.breakers.mcb_3p_32a")
    assert {p.id for p in q.pins} >= {"1", "2", "3", "4", "5", "6"}
    assert q.ref_prefix == "Q"
    mpcb = cat.get("iec.breakers.mpcb_3p")
    assert mpcb.category == "protection.mpcb"
    assert mpcb.ref_prefix == "Q"
    assert {p.id for p in mpcb.pins} >= {"1", "2", "3", "4", "5", "6", "95", "96"}
    assert any(p.type == "polyline" for p in mpcb.primitives)
    nema_mpcb = cat.get("nema.breakers.mpcb_3p")
    assert nema_mpcb.category == "protection.mpcb"
    assert nema_mpcb.ref_prefix == "MCP"
    assert {p.id for p in nema_mpcb.pins} >= {"1", "2", "4", "6", "95", "96"}
    pb = cat.get("iec.operators.pb_no")
    assert pb.properties["contact"] == "NO"
    di = cat.get("adc.click.c0_16nd3")
    assert di.properties["part_number"] == "C0-16ND3"
    assert di.properties["manufacturer"] == "AutomationDirect"
    vfd = cat.get("adc.durapulse.gs21_20p5")
    assert vfd.properties["part_number"] == "GS21-20P5"
    assert vfd.panel.dim_source == "catalog"
    assert vfd.panel.width_mm == 68.0
    assert vfd.panel.height_mm == 128.0
    assert vfd.panel.depth_mm == 125.0
    assert vfd.panel.din_rail is False
    assert vfd.panel.mount == "backplate"
    di = cat.get("adc.click.c0_16nd3")
    assert di.panel.width_mm == 27.0
    assert di.panel.height_mm == 90.3
    assert di.panel.din_rail is True
    brx = cat.get("adc.brx.bx_08nd3")
    assert brx.properties["part_number"] == "BX-08ND3"
    assert brx.properties["manufacturer"] == "AutomationDirect"
    assert {p.id for p in brx.pins} == {"1C", "0", "1", "2", "3", "2C", "4", "5", "6", "7"}
    assert brx.panel.width_mm == 27.5
    assert brx.panel.height_mm == 116.2
    assert brx.panel.depth_mm == 82.6
    assert brx.panel.din_rail is True
    assert brx.panel.dim_source == "catalog"
    brx12 = cat.get("adc.brx.bx_12nd3")
    assert brx12.properties["part_number"] == "BX-12ND3"
    assert {p.id for p in brx12.pins} == {
        "1C", "0", "1", "2", "3", "2C", "4", "5", "6", "7", "3C", "8", "9", "10", "11"
    }
    assert brx12.panel.width_mm == 27.5
    assert brx12.panel.height_mm == 116.2
    sio = cat.get("adc.stride.sio_mb12cdr")
    assert sio.properties["part_number"] == "SIO-MB12CDR"
    assert sio.properties["manufacturer"] == "AutomationDirect"
    assert {p.id for p in sio.pins} >= {"IN0", "IN7", "NO0", "NO3", "NC0", "COM0", "+VS", "-VS", "ETH"}
    assert sio.panel.width_mm == 22.5
    assert sio.panel.height_mm == 120.0
    assert sio.panel.depth_mm == 50.6
    assert sio.panel.din_rail is True
    assert sio.panel.dim_source == "catalog"
    assert any(p.protocol == "Modbus TCP" for p in sio.ports)
    es1 = cat.get("adc.ironhorse.es1_3a_n4")
    assert es1.properties["part_number"] == "ES1-3A-N4"
    assert es1.properties["manufacturer"] == "AutomationDirect"
    assert es1.category == "drive.vfd"
    assert {p.id for p in es1.pins} == {
        "AC1", "AC2", "GND", "U", "V", "W",
        "P7-4", "P7-5", "P7-6", "P7-7", "P7-8", "P7-9", "P7-10",
        "NO", "NC", "COM",
    }
    assert es1.panel.width_mm == 140.47
    assert es1.panel.height_mm == 150.93
    assert es1.panel.depth_mm == 181.61
    assert es1.panel.din_rail is False
    assert es1.panel.mount == "backplate"
    assert es1.panel.dim_source == "catalog"
    assert es1.panel.in_panel is True
    ppc = cat.get("adc.prosense.ppc5_1102")
    assert ppc.properties["part_number"] == "PPC5-1102"
    assert ppc.properties["manufacturer"] == "AutomationDirect"
    assert ppc.category == "control.hmi"
    assert ppc.ref_prefix == "TIC"
    assert {p.id for p in ppc.pins} >= {
        "111", "110", "101", "102", "103",
        "201", "202", "203", "207", "208",
        "211", "210", "209", "212", "ETH",
        "407", "408", "305", "306",
    }
    assert ppc.panel.width_mm == 96.0
    assert ppc.panel.height_mm == 96.0
    assert ppc.panel.depth_mm == 65.0
    assert ppc.panel.din_rail is False
    assert ppc.panel.mount == "door"
    assert ppc.panel.dim_source == "catalog"
    assert any(p.protocol == "Modbus TCP" for p in ppc.ports)
    assert any(p.medium == "RS-485" for p in ppc.ports)
    motor = cat.get("iec.loads.motor_3ph")
    assert motor.panel.in_panel is False
    node = cat.get("iec.connection.node")
    assert node.panel.in_panel is False
    assert cat.get("iec.loads.solenoid").panel.in_panel is False
    assert cat.get("iec.connection.offpage").panel.in_panel is False
    assert cat.get("iec.connection.offpage_from").panel.in_panel is False
    disc = cat.get("iec.protection.disconnect_3p")
    assert disc.panel.in_panel is False


def test_search():
    cat = default_catalog()
    hits = cat.search("hmi")
    assert any(d.library_id.endswith("hmi.panel_7in") for d in hits)


def test_generic_devices_are_iec():
    cat = default_catalog()
    assert cat.get("iec.operators.pb_no").standard == "IEC"
    assert cat.get("iec.contactors.ac3_9a").standard == "IEC"
    assert cat.get("adc.click.c0_16nd3").standard == "OEM"


def test_search_sorts_iec_then_nema_ansi_then_oem(tmp_path):
    from panelcad.library.catalog import Catalog

    path = tmp_path / "nema.yaml"
    path.write_text(
        "library: nema\n"
        "devices:\n"
        "  - id: operators.pb_no\n"
        "    name: Pushbutton NO\n"
        "    category: operator.pushbutton\n"
        "    tags: [button, no]\n"
        "    schematic:\n"
        "      pins:\n"
        "        - { id: '1', x: 0, y: 10, side: left }\n"
        "        - { id: '2', x: 24, y: 10, side: right }\n",
        encoding="utf-8",
    )
    cat = Catalog()
    cat.load_defaults()
    cat.load_file(path)
    pb = cat.get("nema.operators.pb_no")
    assert pb.standard == "NEMA/ANSI"
    assert [p.id for p in pb.pins] == ["1", "2"]

    hits = cat.search("pushbutton")
    ranks = {"IEC": 0, "NEMA/ANSI": 1, "OEM": 2}
    values = [ranks.get(d.standard, 9) for d in hits]
    assert values == sorted(values)
    assert any(d.standard == "IEC" for d in hits)
    assert any(d.standard == "NEMA/ANSI" for d in hits)

    nema_only = cat.search("pushbutton", standard="ANSI")
    assert nema_only
    assert all(d.standard == "NEMA/ANSI" for d in nema_only)


def test_generic_starters_iec_and_nema():
    cat = default_catalog()
    iec = {
        "iec.power.control_transformer": {"H1", "H2", "X1", "X2"},
        "iec.loads.motor_1ph": {"U1", "U2"},
        "iec.loads.motor_3ph": {"U1", "V1", "W1"},
        "iec.contactors.coil": {"A1", "A2"},
        "iec.timers.on_delay": {"A1", "A2", "15", "18"},
        "iec.operators.limit_no": {"13", "14"},
        "iec.operators.limit_nc": {"11", "12"},
        "iec.operators.selector_3pos": {"11", "12", "14"},
        "iec.contactors.aux_no": {"13", "14"},
        "iec.contactors.aux_nc": {"21", "22"},
        "iec.operators.pilot_light": {"X1", "X2"},
        "iec.loads.solenoid": {"A1", "A2"},
        "iec.breakers.mpcb_3p": {"1", "2", "3", "4", "5", "6", "95", "96"},
    }
    nema = {
        "nema.power.control_transformer": {"H1", "H2", "X1", "X2"},
        "nema.loads.motor_1ph": {"T1", "T2"},
        "nema.loads.motor_3ph": {"T1", "T2", "T3"},
        "nema.contactors.coil": {"A1", "A2"},
        "nema.timers.on_delay": {"A1", "A2", "1", "3"},
        "nema.operators.limit_no": {"3", "4"},
        "nema.operators.limit_nc": {"1", "2"},
        "nema.operators.selector_3pos": {"1", "2", "3"},
        "nema.contactors.aux_no": {"3", "4"},
        "nema.contactors.aux_nc": {"1", "2"},
        "nema.operators.pb_no": {"3", "4"},
        "nema.operators.pb_nc": {"1", "2"},
        "nema.operators.estop": {"1", "2"},
        "nema.operators.pilot_light": {"X1", "X2"},
        "nema.protection.fuse_10a": {"1", "2"},
        "nema.protection.overload": {"T1", "T2", "T3", "95", "96"},
        "nema.protection.disconnect_3p": {"L1", "L2", "L3", "T1", "T2", "T3"},
        "nema.terminals.pe": {"A", "B"},
        "nema.breakers.mcb_3p_32a": {"1", "2", "4", "6"},
        "nema.breakers.mcb_1p_16a": {"1", "2"},
        "nema.breakers.mccb_3p_100a": {"1", "2", "6"},
        "nema.breakers.mpcb_3p": {"1", "2", "4", "6", "95", "96"},
        "nema.contactors.ac3_9a": {"L1", "T1", "A1", "A2"},
        "nema.contactors.ac3_32a": {"L1", "T3", "A1"},
        "nema.drives.vfd_1k5": {"L1", "U", "DI1", "COM"},
        "nema.power.psu_24v_5a": {"plus", "minus"},
    }
    for library_id, pins in iec.items():
        device = cat.get(library_id)
        assert device.standard == "IEC", library_id
        assert {p.id for p in device.pins} >= pins, library_id
        if library_id.endswith("contactors.coil"):
            assert {p.id for p in device.pins} == {"A1", "A2"}, library_id
    for library_id, pins in nema.items():
        device = cat.get(library_id)
        assert device.standard == "NEMA/ANSI", library_id
        assert {p.id for p in device.pins} >= pins, library_id
        if library_id.endswith("contactors.coil"):
            assert {p.id for p in device.pins} == {"A1", "A2"}, library_id

def test_save_library_device_roundtrip(tmp_path):
    from panelcad.library.catalog import Catalog, default_catalog

    cat = default_catalog()
    fuse = cat.get("iec.protection.fuse_10a")
    dest = tmp_path / "library" / "user.yaml"
    saved = fuse.model_copy(
        update={
            "id": "fuse_demo",
            "library_id": "local.fuse_demo",
            "name": "Demo inline fuse",
        }
    )
    path = cat.save_device(saved, dest)
    assert path.is_file()
    again = Catalog()
    again.load_file(path)
    d = again.get("local.fuse_demo")
    assert d.name == "Demo inline fuse"
    assert {p.id: p.side for p in d.pins} == {p.id: p.side for p in fuse.pins}


def test_session_save_library_does_not_overwrite_builtin(tmp_path):
    from panelcad import Session

    s = Session.create("Lib")
    s.save(tmp_path / "job.pcad.yaml")
    result = s.save_library_device(
        {
            "id": "my_fuse",
            "name": "My fuse",
            "category": "protection.fuse",
            "library_id": "iec.protection.fuse_10a",
            "ref_prefix": "FU",
            "pins": [{"id": "1", "x": 0, "y": 7, "side": "left"}, {"id": "2", "x": 28, "y": 7, "side": "right"}],
            "width": 28,
            "height": 14,
        }
    )
    assert "iec.protection.fuse_10a" not in str(result["path"])
    assert (tmp_path / "library" / "user.yaml").is_file() or Path(result["path"]).is_file()
    assert s.catalog.get(result["library_id"]).name == "My fuse"


def _pin_rows(device):
    rows: dict[float, list] = {}
    for pin in device.pins:
        if pin.side in {"left", "right"}:
            rows.setdefault(pin.y, []).append(pin)
    return rows


def _through_conductor(device, y: float) -> bool:
    width = device.schematic_width
    for prim in device.primitives:
        if prim.type != "line":
            continue
        if prim.y != y or prim.y2 != y:
            continue
        if min(prim.x, prim.x2) <= 0.5 and max(prim.x, prim.x2) >= width - 0.5:
            return True

    return False


def _blades(device, y: float):
    found = []
    for prim in device.primitives:
        if prim.type != "line":
            continue
        if abs(prim.x - prim.x2) < 4:
            continue
        ys = (prim.y, prim.y2)
        if min(ys) < y - 0.2 and max(ys) >= y - 1.0:
            found.append(prim)
    return found


def test_no_nc_contacts_are_not_through_wires():
    """NO is an open blade; NC is a closed blade onto the right tick — not a bus through the pins."""

    cat = default_catalog()
    no_ids = (
        "iec.operators.pb_no",
        "iec.operators.limit_no",
        "iec.contactors.aux_no",
        "nema.operators.pb_no",
        "nema.operators.limit_no",
        "nema.contactors.aux_no",
    )
    nc_ids = (
        "iec.operators.pb_nc",
        "iec.operators.limit_nc",
        "iec.contactors.aux_nc",
        "iec.protection.ol_contact",
        "iec.operators.estop",
        "nema.operators.pb_nc",
        "nema.operators.limit_nc",
        "nema.contactors.aux_nc",
        "nema.protection.ol_contact",
        "nema.operators.estop",
    )
    for library_id in no_ids + nc_ids:
        device = cat.get(library_id)
        rows = _pin_rows(device)
        assert rows, library_id
        for y, pins in rows.items():
            if len(pins) < 2:
                continue
            assert not _through_conductor(device, y), library_id
            blades = _blades(device, y)
            assert blades, library_id
            reach = max(max(b.x, b.x2) for b in blades)
            if library_id in no_ids:
                assert reach <= device.schematic_width - 7, library_id
            else:
                assert reach >= device.schematic_width - 10, library_id


def test_operator_sits_above_the_contact_pins():
    cat = default_catalog()
    for library_id in (
        "iec.operators.pb_no",
        "iec.operators.pb_nc",
        "iec.operators.estop",
        "nema.operators.pb_no",
        "nema.operators.pb_nc",
        "nema.operators.estop",
    ):
        device = cat.get(library_id)
        pin_y = min(p.y for p in device.pins)
        hats = [p.y for p in device.primitives if p.type == "circle"]
        assert hats, library_id
        assert max(hats) < pin_y - 6, library_id


def _pole_short(device) -> bool:
    """A long horizontal that crosses two pole wires reads as a bus, not a gang bar."""

    verticals = []
    horizontals = []
    for prim in device.primitives:
        if prim.type != "line":
            continue
        if prim.x == prim.x2 and abs(prim.y - prim.y2) > 8:
            verticals.append(prim)
        elif prim.y == prim.y2 and abs(prim.x - prim.x2) > 8:
            horizontals.append(prim)
    for bar in horizontals:
        x0, x1 = sorted((bar.x, bar.x2))
        hits = 0
        for pole in verticals:
            if x0 <= pole.x <= x1 and min(pole.y, pole.y2) < bar.y < max(pole.y, pole.y2):
                hits += 1
        if hits >= 2:
            return True
    return False


def _on_circle(device, x: float, y: float, tol: float = 1.5) -> bool:
    for prim in device.primitives:
        if prim.type != "circle":
            continue
        if abs((x - prim.x) ** 2 + (y - prim.y) ** 2 - prim.r ** 2) <= tol:
            return True
    return False


def _lead_meets_circle(device) -> int:
    hits = 0
    for prim in device.primitives:
        if prim.type != "line":
            continue
        for x, y in ((prim.x, prim.y), (prim.x2, prim.y2)):
            if _on_circle(device, x, y):
                hits += 1
    return hits


def _endpoint_inside_circle(device) -> bool:
    circles = [p for p in device.primitives if p.type == "circle"]
    for prim in device.primitives:
        if prim.type != "line":
            continue
        for x, y in ((prim.x, prim.y), (prim.x2, prim.y2)):
            for circle in circles:
                limit = circle.r - 0.05
                if (x - circle.x) ** 2 + (y - circle.y) ** 2 < limit * limit:
                    return True
    return False


def _slanted_contacts(device):
    found = []
    for prim in device.primitives:
        if prim.type != "line":
            continue
        if abs(prim.x - prim.x2) < 3 or abs(prim.y - prim.y2) < 2:
            continue
        found.append(prim)
    return found


def test_power_symbols_match_print_contacts():
    """Breaker and disconnect poles are switch blades. Overload shows heaters and a load side.

    The DOL power example had three circles for the breaker and an empty overload box.
    """

    cat = default_catalog()
    ganged = (
        "iec.breakers.mcb_3p_32a",
        "iec.breakers.mccb_3p_100a",
        "iec.breakers.mpcb_3p",
        "nema.breakers.mpcb_3p",
        "iec.protection.disconnect_3p",
        "nema.protection.disconnect_3p",
        "iec.contactors.ac3_9a",
        "iec.contactors.ac3_32a",
    )
    for library_id in (
        "iec.breakers.mcb_1p_16a",
        "iec.breakers.mcb_3p_32a",
        "iec.breakers.mccb_3p_100a",
        "iec.breakers.mpcb_3p",
        "nema.breakers.mpcb_3p",
        "iec.protection.disconnect_3p",
        "nema.protection.disconnect_3p",
    ):
        device = cat.get(library_id)
        assert not any(p.type == "circle" for p in device.primitives), library_id
        assert any(_blades(device, y) for y in (14, 16, 18, 20)), library_id
    for library_id in ganged:
        device = cat.get(library_id)
        assert not _pole_short(device), library_id
        assert any(
            p.type == "line" and p.y == p.y2 and abs(p.x - p.x2) >= 12 for p in device.primitives
        ), library_id
    ol = cat.get("iec.protection.overload_9a")
    assert any(p.type == "polyline" for p in ol.primitives)
    mpcb = cat.get("iec.breakers.mpcb_3p")
    assert any(p.type == "polyline" for p in mpcb.primitives)
    assert not any(p.type == "text" and p.text == "F" for p in ol.primitives)
    nema_ol = cat.get("nema.protection.overload")
    assert {p.id for p in nema_ol.pins} >= {"T1", "T2", "T3", "2", "4", "6", "95", "96"}
    assert any(p.side == "bottom" for p in nema_ol.pins)
    plc = cat.get("iec.plc.compact_16dio")
    assert all(p.text != "-A" for p in plc.primitives)
    for library_id in (
        "iec.loads.motor_3ph",
        "nema.loads.motor_3ph",
        "iec.loads.motor_1ph",
        "nema.loads.motor_1ph",
    ):
        motor = cat.get(library_id)
        for prim in motor.primitives:
            if prim.type == "line":
                assert (prim.x, prim.y) != (prim.x2, prim.y2), library_id
        assert not _endpoint_inside_circle(motor), library_id
    for library_id in ("iec.power.control_transformer", "nema.power.control_transformer"):
        xfmr = cat.get(library_id)
        assert sum(1 for p in xfmr.primitives if p.type == "circle") == 2, library_id
        assert _lead_meets_circle(xfmr) >= 4, library_id
    for library_id in (
        "iec.relays.k_4co_24vdc",
        "iec.relays.safety_24vdc",
        "iec.timers.on_delay",
        "nema.timers.on_delay",
    ):
        device = cat.get(library_id)
        assert any(p.type == "circle" for p in device.primitives), library_id
        assert _lead_meets_circle(device) >= 2, library_id
        assert _slanted_contacts(device), library_id
    solenoid = cat.get("iec.loads.solenoid")
    assert any(p.type == "circle" for p in solenoid.primitives)
    assert not any(p.type == "rect" for p in solenoid.primitives)


def test_rung_fuses_are_inline_left_right():
    """JIC H-fuse / IEC 60617 fuse sit in the conductor, not as a drop."""

    cat = default_catalog()
    for library_id in ("iec.protection.fuse_10a", "nema.protection.fuse_10a"):
        fuse = cat.get(library_id)
        sides = {p.id: p.side for p in fuse.pins}
        assert sides["1"] == "left", library_id
        assert sides["2"] == "right", library_id
        ys = {p.y for p in fuse.pins}
        assert len(ys) == 1, library_id
    assert cat.get("iec.operators.limit_no").panel.in_panel is False
    assert cat.get("nema.loads.motor_1ph").panel.in_panel is False
    assert cat.get("nema.protection.disconnect_3p").panel.in_panel is False
    assert cat.get("nema.breakers.mcb_3p_32a").panel.in_panel is True
    assert cat.get("nema.contactors.ac3_9a").panel.in_panel is True
    assert cat.get("nema.drives.vfd_1k5").panel.in_panel is True
    hits = cat.search("transformer")
    ranks = {"IEC": 0, "NEMA/ANSI": 1, "OEM": 2}
    values = [ranks.get(d.standard, 9) for d in hits]
    assert values == sorted(values)
    assert any(d.library_id.startswith("iec.") for d in hits)
    assert any(d.library_id.startswith("nema.") for d in hits)
