"""AutomationDirect, LS Electric, and Yaskawa catalog parts and preferred-vendor circuits."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from typer.testing import CliRunner

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.core.checks import CHECK_IDS
from panelcad.gui.circuit_dialog import CircuitBuilderDialog
from panelcad.gui.part_picker import PartPickerDialog
from panelcad.io.bom import grouped_rows, instance_rows, read_bom_csv
from panelcad.library.parts import default_parts_catalog, normalize_manufacturer
from panelcad.render.page import paint_sheet
from tests.artifacts import artifact_dir
from tests.support import confirm_layout_occupants

ART = Path("artifacts")
STORE = ART


@pytest.fixture(autouse=True)
def _confirm_before_circuit_pack(monkeypatch):
    from panelcad.core import circuit_layout

    orig = circuit_layout.pack_circuit_layouts

    def _pack(session, parts, **kwargs):
        confirm_layout_occupants(session)
        return orig(session, parts, **kwargs)

    monkeypatch.setattr(circuit_layout, "pack_circuit_layouts", _pack)


@pytest.fixture(autouse=True)
def _artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    art = artifact_dir(tmp_path, "part-library-ad-ls-yaskawa")
    raw = os.environ.get("PANELCAD_ARTIFACTS_DIR", "").strip()
    store = (
        Path(raw) / "store" / "part-library-ad-ls-yaskawa"
        if raw
        else tmp_path / "stores" / "part-library-ad-ls-yaskawa"
    )
    monkeypatch.setattr(sys.modules[__name__], "ART", art)
    monkeypatch.setattr(sys.modules[__name__], "STORE", store)


_HOSTS = {
    "AutomationDirect": ("automationdirect.com",),
    "LS Electric": ("ls-electric.com", "lselectricamerica.com"),
    "Yaskawa": ("yaskawa.com",),
}


def _app() -> QApplication:
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    application.setFont(QFont("DejaVu Sans", 10))
    return application


def _by_role(result: dict) -> dict[str, dict]:
    return {str(row["role"]): row for row in result.get("preferred") or []}


def _review_scale(width_mm: float) -> float:
    """A3 review art is at least about 2500 px wide."""

    return max(6.0, 2500.0 / max(float(width_mm), 1.0))


def _render_sheets(session: Session, sheet_ids: list[str], path: Path, scale: float | None = None):
    sheets = [session.project.sheet(sheet_id) for sheet_id in sheet_ids]
    width = max(sheet.page.dimensions()[0] for sheet in sheets)
    if scale is None:
        scale = max(_review_scale(sheet.page.dimensions()[0]) for sheet in sheets)
    height = sum(sheet.page.dimensions()[1] for sheet in sheets)
    image = QImage(int(width * scale), int(height * scale), QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.scale(scale, scale)
    top = 0.0
    for index, sheet in enumerate(sheets):
        painter.save()
        painter.translate(0.0, top)
        paint_sheet(
            painter,
            session.project,
            sheet,
            session.catalog,
            sheet_index=index,
            sheet_count=len(sheets),
        )
        painter.restore()
        top += sheet.page.dimensions()[1]
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path), "PNG")
    assert path.stat().st_size > 5000
    assert image.width() >= 2500, (path.name, image.width(), scale)
    return image


def _render(session: Session, sheet_id: str, path: Path, scale: float | None = None):
    sheet = session.project.sheet(sheet_id)
    width, height = sheet.page.dimensions()
    if scale is None:
        scale = _review_scale(width)
    image = QImage(int(width * scale), int(height * scale), QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.scale(scale, scale)
    paint_sheet(painter, session.project, sheet, session.catalog, sheet_index=0, sheet_count=1)
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path), "PNG")
    assert path.stat().st_size > 5000
    assert image.width() >= 2500, (path.name, image.width(), scale)
    return image


def _crop_mm(image, scale: float, box: tuple[float, float, float, float], path: Path, pad: float = 8.0) -> None:
    left, top, right, bottom = box
    x0 = max(0, int((left - pad) * scale))
    y0 = max(0, int((top - pad) * scale))
    x1 = min(image.width(), int((right + pad) * scale) + 1)
    y1 = min(image.height(), int((bottom + pad) * scale) + 1)
    crop = image.copy(x0, y0, max(1, x1 - x0), max(1, y1 - y0))
    assert crop.save(str(path), "PNG")
    assert path.stat().st_size > 400


def _yaskawa_zoom_crops(session: Session, sheet_id: str, image, scale: float) -> None:
    """Drive terminals, the 3-wire string, and each device tag, large enough to proof."""

    from panelcad.core.ink import text_box
    from panelcad.render.sheet import pin_world
    from panelcad.render.sheet_labels import place_device_tags

    sheet = session.project.sheet(sheet_id)
    drive = next(comp for comp in sheet.components if comp.properties.get("role") == "drive")
    device = session.catalog.get(drive.library_id)
    pins = [
        pin_world(drive, device.pin(pid))
        for pid in ("L1", "L2", "L3", "U", "V", "W", "S1", "S2", "S5", "SN", "H1", "H2")
    ]
    xs = [point[0] for point in pins]
    ys = [point[1] for point in pins]
    _crop_mm(
        image,
        scale,
        (min(xs) - 6, min(ys) - 4, max(xs) + 18, max(ys) + 8),
        ART / "yaskawa-vfd-zoom-terminals.png",
        pad=6,
    )
    roles = {"start", "stop", "fwd_rev", "estop"}
    bodies = []
    for comp in sheet.components:
        if str(comp.properties.get("role") or "") not in roles:
            continue
        symbol = session.catalog.get(comp.library_id)
        drawn = comp.visual_scale()
        bodies.append(
            (
                comp.x,
                comp.y,
                comp.x + float(symbol.schematic_width) * drawn,
                comp.y + float(symbol.schematic_height) * drawn,
            )
        )
    _crop_mm(
        image,
        scale,
        (
            min(box[0] for box in bodies) - 16,
            min(box[1] for box in bodies) - 10,
            max(box[2] for box in bodies) + 16,
            max(box[3] for box in bodies) + 8,
        ),
        ART / "yaskawa-vfd-zoom-control.png",
        pad=4,
    )
    tags = place_device_tags(sheet, session.catalog, project=session.project, sheet_no=1)
    for comp in sheet.components:
        layout = tags.get(comp.id)
        if layout is None:
            continue
        box = text_box(layout[0], layout[1], comp.id, layout[3], layout[2])
        if box is None:
            continue
        safe = "".join(ch if ch.isalnum() else "-" for ch in comp.id)
        _crop_mm(image, scale, box, ART / f"yaskawa-vfd-zoom-tag-{safe}.png", pad=10)


def test_brand_aliases_and_maker_urls():
    assert normalize_manufacturer("adc") == "AutomationDirect"
    assert normalize_manufacturer("LSIS") == "LS Electric"
    assert normalize_manufacturer("ls-electric") == "LS Electric"
    assert normalize_manufacturer("yaskawa") == "Yaskawa"
    parts = default_parts_catalog()
    for name, hosts in _HOSTS.items():
        rows = parts.search(manufacturer=name)
        assert len(rows) >= 10
        for part in rows:
            host = part.source.split("/")[2].lower()
            assert any(allowed in host for allowed in hosts), part.source
            if part.verified == "full":
                assert part.has_footprint()


def test_check_id_includes_catalog_missing_dimensions():
    assert "catalog_missing_dimensions" in CHECK_IDS


def test_preferred_dol_vfd_plc_and_soft_starter():
    ad = Session.create("AD DOL")
    result = ad.insert_circuit(
        kind="dol",
        manufacturer="AutomationDirect",
        fla=4.8,
        voltage=480,
        control_voltage=120,
        control="none",
    )
    picked = _by_role(result)
    assert picked["contactor"]["catalog"] == "GH15BN-3-10A"
    assert picked["overload"]["catalog"] == "RTD32-600"
    assert picked["breaker"]["catalog"] == "FAZ-C6-3-NA-L"
    assert "catalog_fallback" not in picked["breaker"]
    contactor = ad.project.sheet(result["sheet"]).component(picked["contactor"]["id"])
    assert contactor.properties["catalog_source"].startswith("https://cdn.automationdirect.com/")
    assert contactor.library_id.endswith("contactors.ac3_9a")

    local = Session.create("AD local")
    local_result = local.insert_circuit(
        kind="dol",
        manufacturer="adc",
        control="local",
        fla=4.8,
        voltage=480,
    )
    local_picked = _by_role(local_result)
    assert local_picked["contactor"]["catalog"] == "SC-E02PG-24VDC"
    assert local_picked["psu"]["catalog"] == "PSP24-060S"
    assert local_picked["start"]["catalog"] == "GCX1102"
    assert local_picked["stop"]["catalog"] == "GCX1101"

    ad_vfd = Session.create("AD VFD")
    vfd = ad_vfd.insert_circuit(
        kind="vfd",
        drive="generic_1k5",
        manufacturer="AutomationDirect",
        fla=4.8,
        voltage=480,
    )
    assert _by_role(vfd)["drive"]["catalog"] == "GS23-43P0"

    ls = Session.create("LS DOL")
    ls_result = ls.insert_circuit(
        kind="dol",
        manufacturer="ls",
        fla=4.8,
        voltage=480,
        control_voltage=120,
    )
    ls_picked = _by_role(ls_result)
    assert ls_picked["contactor"]["catalog"] == "MC-9b"
    assert "catalog_fallback" not in ls_picked["contactor"]
    assert ls_picked["overload"]["catalog"] == "MT-32/3K5"
    assert ls_picked["breaker"]["catalog"] == "BKN 3P C6A"
    assert "catalog_fallback" not in ls_picked["breaker"]

    ls_vfd = Session.create("LS VFD")
    assert (
        _by_role(
            ls_vfd.insert_circuit(
                kind="vfd",
                drive="generic_1k5",
                manufacturer="LS Electric",
                fla=4.8,
                voltage=480,
            )
        )["drive"]["catalog"]
        == "LSLV0022S100-4"
    )

    plc = Session.create("LS PLC")
    plc_result = plc.insert_circuit(
        kind="plc_motor",
        manufacturer="LSIS",
        control="local",
        fla=4.8,
        voltage=480,
    )
    assert _by_role(plc_result)["plc"]["catalog"] == "XBC-DN32H"

    windings = Session.create("LS star")
    star = windings.insert_circuit(
        kind="star_delta",
        manufacturer="lselectric",
        fla=10,
        voltage=480,
        control_voltage=120,
        overload_placement="windings",
    )
    star_picked = _by_role(star)
    assert star_picked["main"]["catalog"] == "MC-12b"
    assert star_picked["overload"]["catalog"] == "MT-32/3K5"

    ya = Session.create("YA VFD")
    ya_result = ya.insert_circuit(
        kind="vfd",
        drive="generic_1k5",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
    )
    assert _by_role(ya_result)["drive"]["catalog"] == "4005ABA"
    drive = ya.project.sheet(ya_result["sheet"]).component(_by_role(ya_result)["drive"]["id"])
    assert drive.properties["dim_source"] == "catalog"
    assert drive.properties["width_mm"] == 108
    assert drive.properties["height_mm"] == 128

    bare = Session.create("YA DOL")
    bare_result = bare.insert_circuit(
        kind="dol",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
        control_voltage=120,
    )
    bare_picked = _by_role(bare_result)
    assert bare_picked["contactor"]["catalog"] == "GH15BN-3-10A"
    assert bare_picked["contactor"]["catalog_fallback"] == "yes"
    assert bare_picked["contactor"]["catalog_primary"] == "Yaskawa"
    assert bare_picked["overload"]["catalog"] == "RTD32-600"
    assert bare_picked["overload"]["catalog_fallback"] == "yes"
    assert bare_picked["breaker"]["catalog"] == "FAZ-C6-3-NA-L"
    k1 = bare.project.sheet(bare_result["sheet"]).component(bare_picked["contactor"]["id"])
    assert k1.properties["catalog_fallback"] == "yes"
    assert k1.properties["manufacturer"] == "AutomationDirect"

    off = Session.create("YA DOL off")
    off_result = off.insert_circuit(
        kind="dol",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
        control_voltage=120,
        secondary_manufacturer="none",
    )
    assert _by_role(off_result)["contactor"]["catalog"] == ""
    assert "switching.contactor" in _by_role(off_result)["contactor"]["reason"]

    soft = Session.create("YA SS")
    soft_result = soft.insert_circuit(
        kind="soft_starter",
        manufacturer="Yaskawa",
        control="local",
        fla=4.8,
        voltage=480,
    )
    assert _by_role(soft_result)["softstarter"]["catalog"] == ""

    rev = Session.create("AD REV")
    rev_result = rev.insert_circuit(
        kind="reversing",
        manufacturer="AutomationDirect",
        control="local",
        fla=4.8,
        voltage=480,
        control_voltage=120,
        pilot_lights=True,
    )
    rev_picked = _by_role(rev_result)
    assert rev_picked["fwd"]["catalog"] == "GH15BN-3-10A"
    assert rev_picked["rev"]["catalog"] == "GH15BN-3-10A"
    assert rev_picked["fwd_pb"]["catalog"] == "GCX1102"
    assert rev_picked["pilot_fwd"]["catalog"] == "GCX1232-24L"


def test_unknown_secondary_undoes_the_insert():
    session = Session.create("Bad secondary")
    sheets = len(session.project.sheets)
    with pytest.raises(ValueError, match="unknown manufacturer"):
        session.insert_circuit(
            kind="dol",
            manufacturer="Yaskawa",
            secondary_manufacturer="Not A Maker",
            fla=4.8,
            voltage=480,
            control_voltage=120,
        )
    assert len(session.project.sheets) == sheets
    assert session.sheet().components == []


def test_mms_catalog_spelling():
    catalogs = {part.catalog for part in default_parts_catalog().search(manufacturer="LS Electric")}
    assert "MMS-32H-32" in catalogs
    assert not any(code.startswith("MMS-32S") for code in catalogs)


def test_unknown_manufacturer_undoes_the_insert():
    session = Session.create("Unknown")
    sheets = len(session.project.sheets)
    with pytest.raises(ValueError, match="unknown manufacturer"):
        session.insert_circuit(kind="dol", manufacturer="Not A Maker")
    assert len(session.project.sheets) == sheets
    assert session.sheet().components == []


def test_one_undo_removes_circuit_and_skus():
    session = Session.create("Undo SKU")
    result = session.insert_circuit(
        kind="dol",
        manufacturer="AutomationDirect",
        fla=4.8,
        voltage=480,
        control_voltage=120,
    )
    ref = _by_role(result)["contactor"]["id"]
    assert session.project.sheet(result["sheet"]).component(ref).properties["catalog"] == "GH15BN-3-10A"
    session.undo()
    assert all(comp.id != ref for sheet in session.project.sheets for comp in sheet.components)


def test_missing_catalog_dimensions_are_flagged():
    session = Session.create("Dims")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.assign_part("K1", "SC-E02P-110VAC", manufacturer="AutomationDirect")
    flags = [row for row in session.design_checks() if row["check"] == "catalog_missing_dimensions"]
    assert "K1" in {row["ref"] for row in flags}
    report = session.recommend_enclosure()
    assert "K1" in report["catalog_dims_missing"]

    sized = Session.create("GA500 dims")
    ga = sized.insert_circuit(
        kind="vfd",
        drive="generic_1k5",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
    )
    ga_id = _by_role(ga)["drive"]["id"]
    ga_flags = [
        row
        for row in sized.design_checks()
        if row["check"] == "catalog_missing_dimensions" and row["ref"] == ga_id
    ]
    assert ga_flags == []
    assert ga_id not in sized.recommend_enclosure()["catalog_dims_missing"]


def test_bom_export_carries_source_and_import_restores_it(tmp_path: Path):
    session = Session.create("BOM")
    result = session.insert_circuit(
        kind="dol",
        manufacturer="LS Electric",
        fla=4.8,
        voltage=480,
        control_voltage=120,
    )
    ref = _by_role(result)["contactor"]["id"]
    rows = {row["ref"]: row for row in instance_rows(session.project, session.catalog)}
    assert rows[ref]["manufacturer"] == "LS Electric"
    assert rows[ref]["catalog"] == "MC-9b"
    assert "ls-electric.com" in rows[ref]["source"]
    grouped = grouped_rows(session.project, session.catalog)
    match = next(row for row in grouped if row["catalog"] == "MC-9b")
    assert "ls-electric.com" in match["source"]

    dest = tmp_path / "bom.csv"
    session.export_bom(dest)
    loaded = read_bom_csv(dest)
    exported = next(row for row in loaded if row["ref"] == ref)
    assert exported["source"].startswith("https://")
    comp = session.project.sheet(result["sheet"]).component(ref)
    comp.properties.pop("catalog_source", None)
    session.import_bom(dest, place_missing=False)
    assert "ls-electric.com" in comp.properties["catalog_source"]


def test_cli_and_jsonl_search_and_insert(tmp_path: Path):
    runner = CliRunner()
    listed = runner.invoke(app, ["library", "parts", "-m", "Yaskawa", "-q", "4005ABA"])
    assert listed.exit_code == 0, listed.output
    assert "4005ABA" in listed.output
    assert "Yaskawa" in listed.output

    session = Session.create("CLI")
    project = tmp_path / "job.pcad.yaml"
    session.save(project)
    inserted = runner.invoke(
        app,
        [
            "electrical",
            "circuit",
            str(project),
            "--kind",
            "vfd",
            "--drive",
            "generic_1k5",
            "--manufacturer",
            "Yaskawa",
            "--fla",
            "4.8",
            "--voltage",
            "480",
        ],
    )
    assert inserted.exit_code == 0, inserted.output
    assert "4005ABA" in inserted.output
    opened = Session.open(project)
    assert any(
        comp.properties.get("catalog") == "4005ABA"
        for sheet in opened.project.sheets
        for comp in sheet.components
    )

    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "JSONL"})["ok"]
    found = agent.handle({"id": 2, "op": "parts_search", "manufacturer": "lsis", "query": "MC-9b"})
    assert found["ok"], found
    assert found["result"]["parts"][0]["catalog"] == "MC-9b"
    built = agent.handle(
        {
            "id": 3,
            "op": "insert_circuit",
            "kind": "dol",
            "manufacturer": "AutomationDirect",
            "fla": 4.8,
            "voltage": 480,
            "control_voltage": 120,
        }
    )
    assert built["ok"], built
    assert any(row.get("catalog") == "GH15BN-3-10A" for row in built["result"]["preferred"])
    empty = agent.handle(
        {
            "id": 4,
            "op": "insert_circuit",
            "kind": "dol",
            "manufacturer": "Yaskawa",
            "secondary_manufacturer": "none",
            "fla": 4.8,
            "voltage": 480,
            "control_voltage": 120,
        }
    )
    assert empty["ok"], empty
    contactors = [row for row in empty["result"]["preferred"] if row.get("role") == "contactor"]
    assert contactors and contactors[0]["catalog"] == ""


def test_drawing_agent_inserts_preferred_drive():
    steps = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="insert_circuit",
                    args={
                        "kind": "vfd",
                        "drive": "generic_1k5",
                        "manufacturer": "AutomationDirect",
                        "fla": 4.8,
                        "voltage": 480,
                    },
                    tool_call_id="insert-gs",
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="Inserted a GS20.")]),
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = {tool.name: tool for tool in info.function_tools}
        assert "insert_circuit" in tools
        schema = tools["insert_circuit"].parameters_json_schema
        assert "secondary_manufacturer" in schema.get("properties", {})
        return steps.pop(0)

    drawing = open_drawing(name="GS20")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Insert an AutomationDirect VFD.", deps=drawing)
    assert "GS20" in result.output
    catalogs = [
        comp.properties.get("catalog")
        for sheet in drawing.operator.session.project.sheets
        for comp in sheet.components
    ]
    assert "GS23-43P0" in catalogs


def test_circuit_dialog_offers_the_new_brands():
    _app()
    dialog = CircuitBuilderDialog()
    labels = [dialog.manufacturer.itemText(i) for i in range(dialog.manufacturer.count())]
    assert "AutomationDirect" in labels
    assert "LS Electric" in labels
    assert "Yaskawa" in labels
    assert dialog.values()["manufacturer"] is None
    dialog.manufacturer.setCurrentIndex(dialog.manufacturer.findData("Yaskawa"))
    assert dialog.values()["manufacturer"] == "Yaskawa"
    dialog.close()


def _assert_ga500_door_selector_skipped(session: Session, result: dict, fwd=None) -> None:
    """The generic door selector is refused, recorded, and does not abort the insert."""

    from panelcad.library.panel import LayoutNotReadyError

    if fwd is None:
        sheet = session.sheet(result["sheet"])
        fwd = next(item for item in sheet.components if item.properties.get("role") == "fwd_rev")
    findings = [row for row in (result.get("findings") or []) if isinstance(row, dict)]
    skipped = [
        row
        for row in findings
        if row.get("check") == "layout_not_ready" and row.get("action") == "skipped" and row.get("ref") == fwd.id
    ]
    assert skipped, findings
    assert skipped[0]["library_id"].endswith("operators.selector_no")
    assert "not layout-ready" in str(skipped[0].get("message") or "")
    door = next(sheet for sheet in session.project.sheets if sheet.kind == "door_layout")
    assert all(comp.id != fwd.id for comp in door.components)
    checks = [row for row in session.design_checks() if row["check"] == "layout_not_ready" and row["ref"] == fwd.id]
    assert checks
    try:
        session.place(
            fwd.library_id,
            ref=f"{fwd.id}X",
            x=30.0,
            y=30.0,
            sheet_id=door.id,
            properties=dict(fwd.properties),
            snap=False,
        )
    except LayoutNotReadyError as exc:
        assert "not layout-ready" in str(exc)
    else:
        raise AssertionError("the layout gate must still refuse the generic selector")


def test_library_browser_and_brand_circuits_write_artifacts():
    app = _app()
    ART.mkdir(parents=True, exist_ok=True)
    session = Session.create("Browser")
    shots = {
        "AutomationDirect": ART / "library-automationdirect.png",
        "LS Electric": ART / "library-ls-electric.png",
        "Yaskawa": ART / "library-yaskawa.png",
    }
    for name, path in shots.items():
        dialog = PartPickerDialog(session, manufacturer=name)
        dialog.resize(1100, 720)
        dialog.show()
        app.processEvents()
        assert dialog.table.rowCount() >= 1
        assert all(row["manufacturer"] == name for row in dialog._hits)
        image = dialog.grab()
        assert not image.isNull()
        assert image.save(str(path), "PNG")
        assert path.stat().st_size > 1000
        dialog.close()

    jobs = [
        (
            "ad-dol",
            dict(kind="dol", manufacturer="AutomationDirect", control="local", fla=4.8, voltage=480),
            "SC-E02PG-24VDC",
        ),
        (
            "ls-dol",
            dict(
                kind="dol",
                manufacturer="LS Electric",
                control="local",
                fla=4.8,
                voltage=480,
            ),
            "BKN 3P C6A",
        ),
        (
            "yaskawa-vfd",
            dict(
                kind="vfd",
                drive="generic_1k5",
                manufacturer="Yaskawa",
                fla=4.8,
                voltage=480,
                line_contactor=False,
                control="local",
            ),
            "4005ABA",
        ),
    ]
    for stem, kwargs, sku in jobs:
        built = Session.create(stem)
        if stem == "yaskawa-vfd":
            # A door sheet is what makes the generic selector hit the layout gate.
            built.add_sheet("door", title="Door", kind="door_layout", make_active=False)
        result = built.insert_circuit(**kwargs)
        if stem == "yaskawa-vfd":
            _assert_ga500_door_selector_skipped(built, result)
        assert any(row.get("catalog") == sku for row in result["preferred"])
        sheets = [result["sheet"]]
        if result.get("control_sheet"):
            sheets.append(result["control_sheet"])
        schematic = _render_sheets(built, sheets, ART / f"{stem}-schematic.png")
        _render(built, result["layout"]["sheet"], ART / f"{stem}-layout.png")
        if stem == "yaskawa-vfd":
            page_w = built.project.sheet(result["sheet"]).page.dimensions()[0]
            _yaskawa_zoom_crops(built, result["sheet"], schematic, _review_scale(page_w))
        if stem == "ls-dol":
            # MC-9b is 120 V 60 Hz. The shared 24 V ladder has no verified LS
            # coil at 24 V, so the contactor falls back. The 120 V job without
            # local control still selects MC-9b.
            picked = _by_role(result)
            assert picked["contactor"]["catalog"] == "SC-E02PG-24VDC"
            assert picked["contactor"]["catalog_fallback"] == "yes"
            assert picked["contactor"]["catalog_primary"] == "LS Electric"
            assert picked["breaker"]["catalog"] == "BKN 3P C6A"
            assert "catalog_fallback" not in picked["breaker"]
        bom = ART / f"{stem}-bom.csv"
        built.export_bom(bom)
        text = bom.read_text(encoding="utf-8-sig")
        assert sku in text
        header = text.splitlines()[0]
        assert "source" in header
        assert "catalog_fallback" in header
        if stem == "yaskawa-vfd":
            # 3-wire inputs start and stop the drive. Branch protection is the
            # fuse Table 15.4 names. Stop and run stay the verified pushbuttons.
            # The sheet note names FAZ-C6-3-NA-L only to say it is not used.
            assert "FWH-70B" in text
            assert "GCX3300" in text
            assert "part not selected" in text
            assert "GCX1101" in text
            assert "GCX1102" in text
            loaded = read_bom_csv(bom)
            chosen = {
                (row.get("catalog") or "").strip()
                for row in loaded
            } | {(row.get("part_number") or "").strip() for row in loaded}
            assert "GH15BN-3-10A" not in chosen
            assert "FAZ-C6-3-NA-L" not in chosen
            drive_rows = [row for row in loaded if row.get("catalog") == "4005ABA"]
            assert drive_rows and drive_rows[0].get("catalog_fallback", "") in ("", None)
            # read_bom_csv aliases a later "tag" column onto "ref". The export
            # has both, and the operator tag is empty on the fuse, so the
            # reference is read from the ref column itself.
            import csv
            from io import StringIO

            exported = list(csv.DictReader(StringIO(bom.read_text(encoding="utf-8-sig"))))
            fuse_rows = [row for row in exported if (row.get("catalog") or "").strip() == "FWH-70B"]
            assert fuse_rows and all("Eaton/Bussmann" in (row.get("manufacturer") or "") for row in fuse_rows)
            assert {row.get("ref") for row in fuse_rows} == {"FU1"}
            assert all("BH-0122" in (row.get("description") or "") for row in fuse_rows)
            assert "CB1" not in {row.get("ref") for row in exported}
            selector_rows = [row for row in loaded if row.get("catalog") == "GCX3300"]
            assert selector_rows and selector_rows[0].get("catalog_fallback") == "yes"
            assert selector_rows[0].get("catalog_primary") == "Yaskawa"
            estop_rows = [row for row in loaded if "part not selected" in (row.get("part_number") or "").lower() or "part not selected" in (row.get("description") or "").lower()]
            assert estop_rows
            for sku in ("GCX1101", "GCX1102"):
                rows = [row for row in loaded if row.get("catalog") == sku]
                assert rows and rows[0].get("catalog_fallback") == "yes"
                assert rows[0].get("catalog_primary") == "Yaskawa"

    report = Path("docs/PARTS-AD-LS-YASKAWA.md")
    assert report.is_file()
    body = report.read_text(encoding="utf-8")
    assert "GH15BN-3-10A" in body
    assert "MT-32/3K5" in body
    assert "4005ABA" in body
    (ART / "VERIFICATION.md").write_text(body, encoding="utf-8")
    store = STORE
    store.mkdir(parents=True, exist_ok=True)
    for item in ART.iterdir():
        if item.is_file():
            (store / item.name).write_bytes(item.read_bytes())


def _networks(sheet):
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra != rb:
            parent[rb] = ra

    for wire in sheet.wires:
        union(wire.frm, wire.to)
    return find


def _side_pins(session: Session, comp) -> tuple[str, str]:
    device = session.catalog.get(comp.library_id)
    left = next(pin.id for pin in device.pins if pin.side == "left")
    right = next(pin.id for pin in device.pins if pin.side == "right")
    return left, right


@pytest.mark.confirm_generic_layout
def test_sample_layouts_do_not_overlap_and_stay_inside():
    from panelcad.core.layout import footprint_size, rects_overlap

    jobs = [
        dict(kind="dol", manufacturer="AutomationDirect", control="local", fla=4.8, voltage=480),
        dict(kind="dol", manufacturer="LS Electric", control="local", fla=4.8, voltage=480),
        dict(
            kind="vfd",
            drive="generic_1k5",
            manufacturer="Yaskawa",
            fla=4.8,
            voltage=480,
            line_contactor=False,
            control="local",
        ),
    ]
    for kwargs in jobs:
        session = Session.create("sample layout")
        result = session.insert_circuit(**kwargs)
        panel = session.project.sheet(result["layout"]["sheet"])
        enc = panel.enclosure
        assert enc is not None
        boxes = []
        for comp in panel.components:
            device = session.catalog.get(comp.library_id)
            width, height = footprint_size(device, comp)
            assert comp.x >= enc.x - 0.5, comp.id
            assert comp.y >= enc.y - 0.5, comp.id
            assert comp.x + width <= enc.x + enc.width_mm + 0.5, comp.id
            assert comp.y + height <= enc.y + enc.height_mm + 0.5, (comp.id, comp.y, height, enc.height_mm)
            boxes.append((comp.id, comp.x, comp.y, width, height))
        for index, left in enumerate(boxes):
            for right in boxes[index + 1 :]:
                assert not rects_overlap(left[1], left[2], left[3], left[4], right[1], right[2], right[3], right[4]), (
                    kwargs["manufacturer"],
                    left,
                    right,
                )
        if kwargs["kind"] == "vfd":
            drive = next(comp for comp in panel.components if comp.properties.get("role") == "drive")
            assert drive.properties.get("mount") == "backplate"
            assert str(drive.properties.get("din_rail")).lower() in {"false", "0", "no"}
            assert float(drive.properties["keepout_left_mm"]) == 30
            assert float(drive.properties["keepout_right_mm"]) == 30
            assert float(drive.properties["keepout_top_mm"]) == 100
            assert float(drive.properties["keepout_bottom_mm"]) == 100
            from panelcad.library.panel import effective_footprint

            drive_fp = effective_footprint(session.catalog.get(drive.library_id), drive)
            assert drive_fp.width_mm == 108
            assert drive_fp.height_mm == 128
            assert drive_fp.depth_mm == 154
            from panelcad.core.layout import enclosure_drawn_size, rail_height

            fuse = next(comp for comp in panel.components if comp.properties.get("role") == "breaker")
            fuse_fp = effective_footprint(session.catalog.get(fuse.library_id), fuse)
            assert fuse_fp.din_rail is False
            assert fuse_fp.mount == "backplate"
            assert fuse_fp.width_mm == pytest.approx(53.98 * 3, abs=0.02)
            assert fuse_fp.height_mm == pytest.approx(164.08, abs=0.02)
            assert fuse_fp.depth_mm == pytest.approx(68.20, abs=0.02)
            assert int(fuse.properties.get("pole_count") or 0) == 3
            assert fuse.properties.get("pole_label") == "BH-0122"
            assert fuse.properties.get("catalog") == "FWH-70B"
            assert "mcb_3p_32a" not in fuse.library_id
            scale = drive.visual_scale()
            drive_w, drive_h = footprint_size(session.catalog.get(drive.library_id), drive)
            keepout = (
                drive.x - 30.0 * scale,
                drive.y - 100.0 * scale,
                drive_w + 60.0 * scale,
                drive_h + 200.0 * scale,
            )
            drawn_w, _drawn_h, drawn_s = enclosure_drawn_size(enc)
            fuse_w, fuse_h = footprint_size(session.catalog.get(fuse.library_id), fuse)
            assert abs((fuse.x + fuse_w / 2.0) - (enc.x + drawn_w / 2.0)) < 1.0
            for comp in panel.components:
                if comp.id == drive.id:
                    continue
                device = session.catalog.get(comp.library_id)
                width, height = footprint_size(device, comp)
                # A shared edge with the keep-out counts as a hit.
                assert not rects_overlap(
                    comp.x,
                    comp.y,
                    width,
                    height,
                    keepout[0] - 1.0,
                    keepout[1] - 1.0,
                    keepout[2] + 2.0,
                    keepout[3] + 2.0,
                ), comp.id
                fp = effective_footprint(device, comp)
                if not fp.din_rail:
                    continue
                assert panel.rails, comp.id
                rail = min(panel.rails, key=lambda item: abs(item.y - (comp.y + height / 2.0)))
                rh = rail_height(rail)
                assert comp.x >= rail.x - 0.2, comp.id
                assert comp.x + width <= rail.x + rail.length_mm + 0.2, comp.id
                assert comp.y >= rail.y - rh / 2.0 - 0.2, comp.id
                assert comp.y + height <= rail.y + rh / 2.0 + 0.2, comp.id
            assert drawn_s > 0


def _assert_traces_clear_foreign_pins(session: Session, sheet_id: str, clearance: float = 1.5) -> None:
    """A trace may leave its own pin. It must not pass through any other pin dot."""

    from math import hypot

    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import route_sheet_wires

    sheet = session.sheet(sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    pins: list[tuple[str, float, float]] = []
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        for pin in device.pins:
            wx, wy = pin_world(comp, pin)
            pins.append((f"{comp.id}.{pin.id}", wx, wy))
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        ends = {wire.frm, wire.to}
        for a, b in zip(pts, pts[1:]):
            dx, dy = b.x - a.x, b.y - a.y
            length2 = dx * dx + dy * dy
            for key, wx, wy in pins:
                if key in ends:
                    continue
                if length2 < 1e-9:
                    dist = hypot(wx - a.x, wy - a.y)
                else:
                    t = max(0.0, min(1.0, ((wx - a.x) * dx + (wy - a.y) * dy) / length2))
                    dist = hypot(wx - (a.x + t * dx), wy - (a.y + t * dy))
                assert dist >= clearance, (sheet_id, wire.net, wire.frm, wire.to, key, round(dist, 2))


def _notes_wire_clearance(session: Session, sheet) -> float:
    """Shortest distance from the notes rectangle to a wire segment."""

    from panelcad.render.painter import notes_box
    from panelcad.render.wiring import route_sheet_wires

    note = next(item for item in sheet.components if str(item.properties.get("role") or "") == "notes")
    width, height = notes_box(note, note.visual_scale())
    left, top, right, bottom = note.x, note.y, note.x + width, note.y + height
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    nearest = 1e9
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            nearest = min(nearest, _seg_box_distance(a.x, a.y, b.x, b.y, left, top, right, bottom))
    return nearest


def _seg_box_distance(
    x1: float, y1: float, x2: float, y2: float, left: float, top: float, right: float, bottom: float
) -> float:
    if abs(y1 - y2) <= 0.4:
        y = y1
        span0, span1 = sorted((x1, x2))
        if bottom >= y >= top and not (span1 < left or span0 > right):
            return 0.0
        if bottom >= y >= top:
            return min(abs(span0 - left), abs(span0 - right), abs(span1 - left), abs(span1 - right))
        dy = min(abs(y - top), abs(y - bottom))
        if span1 < left or span0 > right:
            dx = left - span1 if span1 < left else span0 - right
            return (dx * dx + dy * dy) ** 0.5
        return dy
    if abs(x1 - x2) <= 0.4:
        x = x1
        span0, span1 = sorted((y1, y2))
        if right >= x >= left and not (span1 < top or span0 > bottom):
            return 0.0
        if right >= x >= left:
            return min(abs(span0 - top), abs(span0 - bottom), abs(span1 - top), abs(span1 - bottom))
        dx = min(abs(x - left), abs(x - right))
        if span1 < top or span0 > bottom:
            dy = top - span1 if span1 < top else span0 - bottom
            return (dx * dx + dy * dy) ** 0.5
        return dx
    return 1e9


def _assert_power_column_vertical(session: Session, sheet) -> None:
    """Phase pins that are on the sheet share three vertical columns."""

    from panelcad.render.sheet import pin_world

    columns = (
        ("breaker", ("2", "4", "6")),
        ("contactor", ("L1", "L2", "L3")),
        ("drive", ("L1", "L2", "L3")),
        ("motor", ("T1", "T2", "T3")),
    )
    worlds: list[list[float]] = []
    for role, pins in columns:
        comp = next((item for item in sheet.components if item.properties.get("role") == role), None)
        if comp is None:
            continue
        device = session.catalog.get(comp.library_id)
        worlds.append([pin_world(comp, device.pin(pid))[0] for pid in pins])
    assert len(worlds) >= 3
    for index in range(3):
        xs = [row[index] for row in worlds]
        assert max(xs) - min(xs) < 0.5, (index, xs)


def _assert_bodies(session: Session, sheet_id: str) -> None:
    """Reuse the circuit-builder body check without a ``tests`` package import.

    The full suite puts ``tests/`` on ``sys.path``, so ``import tests.*`` fails.
    """

    import importlib.util
    import sys
    from pathlib import Path

    module = next(
        (
            mod
            for name, mod in sys.modules.items()
            if name == "test_circuit_builder" or name.endswith(".test_circuit_builder")
        ),
        None,
    )
    if module is None or not hasattr(module, "_assert_drawn_segments_match_pins"):
        path = Path(__file__).resolve().parent / "test_circuit_builder.py"
        spec = importlib.util.spec_from_file_location("panelcad_test_circuit_builder", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module._assert_drawn_segments_match_pins(session, sheet_id, mix_numbers=False, bodies=True)


def test_sample_dol_and_vfd_control_netlists():
    for maker in ("AutomationDirect", "LS Electric"):
        session = Session.create(maker)
        result = session.insert_circuit(
            kind="dol", manufacturer=maker, control="local", fla=4.8, voltage=480
        )
        sheet = session.sheet(result["control_sheet"])
        find = _networks(sheet)
        assert find("24V") != find("0V")
        coil = next(comp for comp in sheet.components if comp.properties.get("role") == "coil")
        start = next(comp for comp in sheet.components if comp.properties.get("role") == "start")
        seal = next(comp for comp in sheet.components if comp.id.endswith("-13"))
        ol = next(comp for comp in sheet.components if comp.properties.get("role") == "ol_contact")
        assert any(f"{coil.id}.A1" in (wire.frm, wire.to) for wire in sheet.wires)
        assert any(f"{coil.id}.A2" in (wire.frm, wire.to) for wire in sheet.wires)
        assert find(f"{coil.id}.A1") != find(f"{coil.id}.A2")
        assert find(f"{coil.id}.A2") == find("0V")
        assert find(f"{coil.id}.A1") != find("24V")
        start_in, start_out = _side_pins(session, start)
        seal_in, seal_out = _side_pins(session, seal)
        ol_in, ol_out = _side_pins(session, ol)
        assert find(f"{start.id}.{start_in}") == find(f"{seal.id}.{seal_in}")
        assert find(f"{start.id}.{start_out}") == find(f"{seal.id}.{seal_out}")
        assert find(f"{start.id}.{start_in}") != find(f"{start.id}.{start_out}")
        assert find(f"{ol.id}.{ol_out}") == find(f"{coil.id}.A1")
        assert find(f"{ol.id}.{ol_in}") == find(f"{start.id}.{start_out}")
        assert find(f"{ol.id}.{ol_in}") != find(f"{ol.id}.{ol_out}")
        _assert_bodies(session, result["control_sheet"])
        _assert_bodies(session, result["sheet"])
        _assert_traces_clear_foreign_pins(session, result["control_sheet"])
        _assert_traces_clear_foreign_pins(session, result["sheet"])

    session = Session.create("Yaskawa VFD")
    session.add_sheet("door", title="Door", kind="door_layout", make_active=False)
    result = session.insert_circuit(
        kind="vfd",
        drive="generic_1k5",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
        line_contactor=False,
        control="local",
    )
    sheet = session.sheet(result["sheet"])
    find = _networks(sheet)
    assert "24V" not in {wire.net for wire in sheet.wires}
    drive = next(comp for comp in sheet.components if comp.properties.get("role") == "drive")
    assert drive.library_id.endswith("drives.ga500")
    for pin in ("S1", "S2", "S5", "SN", "SC", "SP", "H1", "H2", "HC", "L1", "L2", "L3", "U", "V", "W"):
        assert any(f"{drive.id}.{pin}" in (wire.frm, wire.to) for wire in sheet.wires), pin
    assert "DI1" not in {pin.id for pin in session.catalog.get(drive.library_id).pins}
    assert "COM" not in {pin.id for pin in session.catalog.get(drive.library_id).pins}
    start = next(item for item in sheet.components if item.properties.get("role") == "start")
    stop = next(item for item in sheet.components if item.properties.get("role") == "stop")
    estop = next(item for item in sheet.components if item.properties.get("role") == "estop")
    fwd = next(item for item in sheet.components if item.properties.get("role") == "fwd_rev")
    start_l, start_r = _side_pins(session, start)
    stop_l, stop_r = _side_pins(session, stop)
    fwd_l, fwd_r = _side_pins(session, fwd)
    # Figure 2.56: S1 is after the run NO, S2 taps the node between run and stop, S5 is separate.
    assert find(f"{drive.id}.S1") == find(f"{start.id}.{start_l}")
    assert find(f"{drive.id}.S2") == find(f"{start.id}.{start_r}")
    assert find(f"{drive.id}.S2") == find(f"{stop.id}.{stop_l}")
    assert find(f"{drive.id}.S1") != find(f"{drive.id}.S2")
    assert find(f"{drive.id}.SN") == find(f"{stop.id}.{stop_r}")
    assert find(f"{drive.id}.SN") == find(f"{fwd.id}.{fwd_r}")
    assert find(f"{drive.id}.S5") == find(f"{fwd.id}.{fwd_l}")
    assert find(f"{drive.id}.S5") != find(f"{drive.id}.S1")
    assert find(f"{drive.id}.S5") != find(f"{drive.id}.SN")
    assert find(f"{drive.id}.SC") == find(f"{drive.id}.SP")
    assert find(f"{drive.id}.SC") != find(f"{drive.id}.SN")
    # Safe Disable is not in series with the run string.
    assert find(f"{drive.id}.H1") != find(f"{drive.id}.S1")
    assert find(f"{drive.id}.H1") != find(f"{drive.id}.S2")
    assert find(f"{drive.id}.H1") != find(f"{drive.id}.H2")
    assert find(f"{drive.id}.HC") != find(f"{drive.id}.H1")
    assert find(f"{estop.id}.11") == find(f"{drive.id}.H1")
    assert find(f"{estop.id}.21") == find(f"{drive.id}.H2")
    assert find(f"{estop.id}.12") == find(f"{drive.id}.HC")
    assert find(f"{estop.id}.22") == find(f"{drive.id}.HC")
    assert not any(item.properties.get("role") == "contactor" for item in sheet.components)
    assert not any(row["check"] == "unwired_coil" for row in session.design_checks())
    breaker = next(item for item in sheet.components if item.properties.get("role") == "breaker")
    assert breaker.library_id.endswith("protection.fuse_3p")
    assert breaker.properties.get("catalog") == "FWH-70B"
    from panelcad.core.refs import resolve_ref_prefix, sheet_drawing_standard

    fuse_device = session.catalog.get(breaker.library_id)
    fuse_prefix = resolve_ref_prefix(
        fuse_device,
        standard=sheet_drawing_standard(session.project, sheet),
        maps=session.catalog.ref_prefix_maps,
        overrides=session.project.ref_prefixes,
    )
    assert breaker.id == f"{fuse_prefix}1"
    assert fuse_prefix == "FU"
    assert "BH-0122" in str(breaker.properties.get("description") or "")
    assert "mcb_3p_32a" not in breaker.library_id
    for pin in ("1", "3", "5"):
        assert not any(f"{breaker.id}.{pin}" in (wire.frm, wire.to) for wire in sheet.wires)
    rating = str(drive.properties.get("rating_lines") or "")
    assert "HD 3 HP 4.8 A" in rating
    assert "ND 3 HP 5.4 A" in rating
    assert "1.5kW" not in rating
    drawn = " ".join(
        prim.text or "" for prim in session.catalog.get(drive.library_id).primitives if getattr(prim, "text", None)
    )
    assert "1.5kW" not in drawn
    for name in ("S1", "S2", "S5", "SN", "SC", "SP", "H1", "H2", "HC", "L1", "U"):
        assert name in drawn.split()
    assert fwd.library_id.endswith("operators.selector_no")
    _assert_ga500_door_selector_skipped(session, result, fwd=fwd)
    from panelcad.render.sheet import pin_world

    device = session.catalog.get(drive.library_id)
    h1y = pin_world(drive, device.pin("H1"))[1]
    h2y = pin_world(drive, device.pin("H2"))[1]
    hcy = pin_world(drive, device.pin("HC"))[1]
    estop_dev = session.catalog.get(estop.library_id)
    es_ys = sorted(pin_world(estop, pin)[1] for pin in estop_dev.pins if pin.side == "left")
    assert abs((h2y - h1y) - (es_ys[1] - es_ys[0])) < 0.4
    assert hcy - h2y >= 8.0
    note = next(item for item in sheet.components if item.library_id.endswith("notes.block"))
    text = str(note.properties.get("text") or "")
    assert "TOEPYAIGA5002A" in text and "291" in text
    assert "TOEPC7106173TA" in text and "Figure 18.2" in text
    assert "H1-05" in text and "A1-03" in text and "SC-SP" in text
    assert "FWH-70B" in text and "Table 15.4" in text
    assert "Figure 7.5" in text
    assert "does not call this terminal an E-stop" in text
    assert "Part not selected" in text
    assert "GCX3300" in text
    motor = next(item for item in sheet.components if item.properties.get("role") == "motor")
    assert float(motor.properties["hp"]) == 3
    assert float(motor.properties["kW"]) == 2.2
    assert float(motor.properties["FLA"]) == 4.8
    assert str(motor.properties["voltage_label"]) == "460/480"
    from panelcad.core.pins import rating_captions

    caption = " ".join(rating_captions(motor))
    assert "3 hp" in caption and "460/480" in caption and "FLA 4.8 A" in caption
    assert estop.properties.get("part_number") == "part not selected"
    assert fwd.properties.get("catalog") == "GCX3300"
    from panelcad.core.ink import device_ref_layout, text_box
    from panelcad.render.sheet_labels import device_tag_faults, sheet_conductor_boxes
    from panelcad.render.wire_labels import boxes_overlap

    tag_faults = device_tag_faults(session.project, session.catalog, sheet, sheet_no=1)
    assert tag_faults == [], tag_faults
    from panelcad.render.painter import notes_box
    from panelcad.render.sheet_labels import place_device_tags

    placed = place_device_tags(sheet, session.catalog, project=session.project, sheet_no=1)
    estop_tag = placed.get(estop.id)
    drive_tag = placed.get(drive.id)
    assert estop_tag is not None and drive_tag is not None
    estop_box = text_box(estop_tag[0], estop_tag[1], estop.id, estop_tag[3], estop_tag[2])
    drive_box = text_box(drive_tag[0], drive_tag[1], drive.id, drive_tag[3], drive_tag[2])
    assert estop_box is not None and drive_box is not None
    assert estop_box[3] <= estop.y - 0.4
    assert estop.y - estop_box[3] < 8.0
    assert estop_box[0] < estop.x
    assert estop.x - estop_box[0] < 18.0
    note_w, note_h = notes_box(note, note.visual_scale())
    note_box = (note.x, note.y, note.x + note_w, note.y + note_h)
    assert not boxes_overlap(estop_box, note_box)
    assert drive_box[3] <= drive.y
    assert drive.y - drive_box[3] < 8.0
    assert drive_box[0] < drive.x
    motor_dev = session.catalog.get(motor.library_id)
    old = device_ref_layout(motor_dev, motor, ref_align="start")
    assert old is not None
    old_box = text_box(old[0], old[1], motor.id, old[3], old[2])
    assert old_box is not None
    conductors = sheet_conductor_boxes(sheet, session.catalog)
    assert any(boxes_overlap(old_box, box) for _name, box in conductors), old_box
    report = Path("docs/PARTS-AD-LS-YASKAWA.md").read_text(encoding="utf-8")
    assert "TOEPYAIGA5002A" in report and "Figure 2.56" in report and "Figure 18.2" in report
    assert "Table 15.4" in report and "Figure 7.5" in report and "GCX3300" in report
    assert "BH-0122" in report and "data sheet 1200" in report
    _assert_power_column_vertical(session, sheet)
    from panelcad.core.ga500 import sheet_close_parallels, sheet_net_crossings

    assert sheet_net_crossings(sheet, session.catalog) == []
    assert sheet_close_parallels(sheet, session.catalog) == []
    assert _notes_wire_clearance(session, sheet) >= 6.0
    _assert_bodies(session, result["sheet"])
    _assert_traces_clear_foreign_pins(session, result["sheet"])


@pytest.mark.confirm_generic_layout
def test_ga500_fuse_tag_follows_the_category_prefix():
    """A project override of protection.fuse renames the sample. The builder does not keep CB."""

    session = Session.create("fuse prefix")
    session.project.ref_prefixes["protection.fuse"] = "XF"
    result = session.insert_circuit(
        kind="vfd",
        drive="generic_1k5",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
        line_contactor=False,
        control="local",
    )
    sheet = session.sheet(result["sheet"])
    fuse = next(item for item in sheet.components if item.properties.get("role") == "breaker")
    assert fuse.id == "XF1"
    assert fuse.library_id.endswith("protection.fuse_3p")
    assert not any(item.id == "CB1" for item in sheet.components)
    assert any(end.startswith("XF1.") for wire in sheet.wires for end in (wire.frm, wire.to))
    panel = session.project.sheet(result["layout"]["sheet"])
    assert any(item.id == "XF1" for item in panel.components)
    assert not any(item.id == "CB1" for item in panel.components)
    preferred = {row["id"]: row for row in result["preferred"]}
    assert preferred["XF1"]["catalog"] == "FWH-70B"


def test_a_placed_device_without_a_tag_fails(monkeypatch):
    session = Session.create("missing tag")
    session.place("nema.operators.estop_2nc", ref="ES1", x=80, y=80)
    sheet = session.sheet()
    monkeypatch.setattr("panelcad.render.sheet_labels.place_device_tags", lambda *_args, **_kwargs: {})
    from panelcad.render.sheet_labels import device_tag_faults

    faults = device_tag_faults(session.project, session.catalog, sheet, sheet_no=1)
    assert any("ES1 has no visible tag" in row for row in faults), faults
