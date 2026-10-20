"""Verified manufacturer part library: schema, search, assign, BOM, overlay."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.checks import CHECK_IDS
from panelcad.examples.dol import build_dol
from panelcad.io.bom import instance_rows
from panelcad.library.catalog import default_catalog
from panelcad.library.panel import effective_footprint
from panelcad.library.parts import (
    ADJUSTABLE_CATEGORIES,
    CatalogPart,
    default_parts_catalog,
    write_parts_csv,
)


def test_check_id_includes_unverified_part():
    assert "unverified_part" in CHECK_IDS


def test_every_catalog_entry_has_official_source_and_known_symbol():
    symbols = default_catalog()
    parts = default_parts_catalog()
    assert len(parts.parts) >= 50
    unknown = parts.validate_against_symbols(symbols.devices)
    assert unknown == []
    for part in parts.parts.values():
        assert part.source.startswith("http://") or part.source.startswith("https://")
        assert part.verified in {"full", "partial"}
        assert part.manufacturer
        assert part.catalog
        assert part.library_id in symbols.devices
        assert part.category


def test_catalog_part_rejects_unknown_and_fabricated_fields():
    with pytest.raises(ValidationError):
        CatalogPart.model_validate(
            {
                "manufacturer": "Allen-Bradley",
                "catalog": "NO-SUCH",
                "library_id": "iec.contactors.ac3_9a",
                "category": "switching.contactor",
                "verified": "full",
                "source": "https://www.rockwellautomation.com/en-us/",
                "invented_amps": 99,
            }
        )
    with pytest.raises(ValidationError):
        CatalogPart.model_validate(
            {
                "manufacturer": "Allen-Bradley",
                "catalog": "NO-SUCH",
                "library_id": "iec.contactors.ac3_9a",
                "category": "switching.contactor",
                "verified": "full",
                "source": "not-a-url",
            }
        )


def test_suffix_rule_expands_documented_100c_coils():
    parts = default_parts_catalog()
    d10 = parts.get("100-C09D10", "Allen-Bradley")
    assert d10.coil_voltage.startswith("110VAC")
    assert d10.heat_w == 3.4
    assert d10.width_mm == 45
    ej = parts.get("100-C09EJ10")
    assert ej.coil_voltage == "24VDC"
    assert ej.heat_w == 2.4


def test_terminal_vendor_articles_are_reused_not_copied():
    parts = default_parts_catalog()
    ut = parts.get("3044076", "Phoenix Contact")
    assert ut.library_id == "iec.terminals.x_2p5"
    assert ut.verified == "full"
    assert "phoenixcontact.com" in ut.source
    assert ut.notes.startswith("Loaded from")


def test_search_parts_by_manufacturer_catalog_and_rating():
    s = Session.create("Parts")
    ab = s.search_parts("1489-M3C160")
    assert len(ab) == 1
    assert ab[0]["manufacturer"] == "Allen-Bradley"
    assert ab[0]["verified"] == "full"
    assert ab[0]["width_mm"] == 52.5
    contactors = s.search_parts("9 A", category="switching.contactor", manufacturer="Allen-Bradley")
    assert any(row["catalog"] == "100-C09D10" for row in contactors)
    siemens = s.search_parts("3RT2015", manufacturer="Siemens")
    assert siemens
    assert all(row["manufacturer"] == "Siemens" for row in siemens)
    range_hits = s.search_parts("", category="protection.mpcb", amps_min=2.2, amps_max=3.2)
    catalogs = {row["catalog"] for row in range_hits}
    assert "3RV2011-1DA25" in catalogs
    assert "3RV2011-1BA10" not in catalogs


@pytest.mark.confirm_generic_layout
def test_assign_part_is_one_undo_and_keeps_library_id():
    s = Session.create("Assign")
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="enclosure")
    before = s.sheet().component("K1").library_id
    result = s.assign_part("K1", "100-C09D10", manufacturer="Allen-Bradley")
    assert result["library_id"] == before == "iec.contactors.ac3_9a"
    assert result["verified"] == "full"
    power = s.project.sheet("main").component("K1")
    panel = s.project.sheet("enclosure").component("K1")
    assert power.properties["catalog"] == "100-C09D10"
    assert panel.properties["catalog"] == "100-C09D10"
    assert power.properties["manufacturer"] == "Allen-Bradley"
    assert power.properties["heat_w"] == 3.4
    assert power.library_id == "iec.contactors.ac3_9a"
    assert s.can_undo()
    assert s.undo()
    assert "catalog" not in s.project.sheet("main").component("K1").properties
    assert s.redo()
    assert s.project.sheet("main").component("K1").properties["catalog"] == "100-C09D10"


def test_assign_updates_bom_and_catalog_footprint():
    s = build_dol()
    s.assign_part("Q1", "1489-M3C160")
    s.assign_part("K1", "100-C09D10", manufacturer="Allen-Bradley")
    rows = {row["ref"]: row for row in instance_rows(s.project, s.catalog) if row["ref"] in {"Q1", "K1"}}
    assert rows["Q1"]["manufacturer"] == "Allen-Bradley"
    assert rows["Q1"]["part_number"] == "1489-M3C160"
    assert rows["Q1"]["catalog"] == "1489-M3C160"
    assert "1489-M" in rows["Q1"]["description"]
    assert rows["Q1"]["width_mm"] == "52.5"
    assert rows["Q1"]["height_mm"] == "111"
    assert rows["Q1"]["dim_source"] == "catalog"
    assert rows["K1"]["part_number"] == "100-C09D10"
    q1 = next(c for sheet in s.project.sheets for c in sheet.components if c.id == "Q1")
    device = s.catalog.get(q1.library_id)
    fp = effective_footprint(device, q1)
    assert fp.width_mm == 52.5
    assert fp.height_mm == 111
    assert fp.depth_mm == 69
    assert fp.dim_source == "catalog"


def test_unverified_part_check_is_info_for_partial_entries():
    s = Session.create("Partial")
    s.place("iec.protection.overload_9a", ref="F1", x=20, y=20)
    assert not [
        row for row in s.design_checks() if row["check"] == "unverified_part" and row["ref"] == "F1"
    ]
    s.assign_part("F1", "193-T1AC10")
    rows = [row for row in s.design_checks() if row["check"] == "unverified_part"]
    assert rows
    assert rows[0]["severity"] == "info"
    assert rows[0]["ref"] == "F1"
    assert "193-T1AC10" in rows[0]["message"]


def test_overlay_project_parts_win(tmp_path: Path):
    overlay = tmp_path / "library" / "parts"
    overlay.mkdir(parents=True)
    overlay.joinpath("shop.yaml").write_text(
        """
kind: manufacturer_parts
manufacturer: Allen-Bradley
parts:
  - catalog: "100-C09D10"
    library_id: iec.contactors.ac3_9a
    category: switching.contactor
    description: Shop-corrected 100-C09D10
    verified: partial
    source: https://www.rockwellautomation.com/en-us/products/details.100-C09D10.html
    amps: 9
    poles: 3
""",
        encoding="utf-8",
    )
    project = tmp_path / "job.pcad.yaml"
    s = Session.create("Overlay")
    s.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    s.save(project)
    opened = Session.open(project)
    part = opened.parts.get("100-C09D10", "Allen-Bradley")
    assert part.description == "Shop-corrected 100-C09D10"
    assert part.verified == "partial"
    builtin = default_parts_catalog()
    assert builtin.get("100-C09D10").description != part.description


def test_overlay_env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    extra.joinpath("eaton_fix.yaml").write_text(
        """
kind: manufacturer_parts
manufacturer: Eaton
parts:
  - catalog: "FAZ-C10/1-NA"
    library_id: iec.breakers.mcb_1p_16a
    category: protection.breaker
    description: Overlay FAZ note
    verified: full
    source: https://www.eaton.com/us/en-us/skuPage.102087.html
    amps: 10
    poles: 1
    width_mm: 17.7
    height_mm: 105
    depth_mm: 75.5
    mounting: din
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELCAD_LIBRARY_PATH", str(extra))
    parts = default_parts_catalog()
    assert parts.get("FAZ-C10/1-NA").description == "Overlay FAZ note"


def test_agent_parts_search_and_assign():
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "SKU"})
    assert created["ok"]
    agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.breakers.mcb_1p_16a",
            "ref": "Q1",
            "x": 20,
            "y": 20,
        }
    )
    search = agent.handle({"id": 3, "op": "parts_search", "query": "1489-M1C160"})
    assert search["ok"]
    assert search["result"]["parts"]
    assert search["result"]["parts"][0]["catalog"] == "1489-M1C160"
    assigned = agent.handle(
        {
            "id": 4,
            "op": "assign_part",
            "ref": "Q1",
            "catalog": "1489-M1C160",
            "manufacturer": "Allen-Bradley",
        }
    )
    assert assigned["ok"]
    assert assigned["result"]["catalog"] == "1489-M1C160"
    assert agent.session.sheet().component("Q1").properties["part_number"] == "1489-M1C160"


def test_full_adjustable_parts_have_amp_range():
    parts = default_parts_catalog()
    adjustable = [p for p in parts.parts.values() if p.verified == "full" and p.category in ADJUSTABLE_CATEGORIES]
    assert adjustable, "expected at least one verified:full adjustable part"
    for part in adjustable:
        assert part.amps_min is not None, part.catalog
        assert part.amps_max is not None, part.catalog
        assert part.amps == part.amps_max, part.catalog
        assert part.amps_min <= part.amps_max, part.catalog


def test_schema_rejects_full_adjustable_without_range():
    with pytest.raises(ValidationError, match="amps_min"):
        CatalogPart.model_validate(
            {
                "manufacturer": "Siemens",
                "catalog": "3RV-FAKE",
                "library_id": "iec.breakers.mpcb_3p",
                "category": "protection.mpcb",
                "verified": "full",
                "source": "https://mall.industry.siemens.com/example",
                "amps": 3.2,
            }
        )


def test_siemens_3rv_mpcb_fields_and_assign_writes_range():
    parts = default_parts_catalog()
    da25 = parts.get("3RV2011-1DA25", "Siemens")
    assert da25.verified == "full"
    assert da25.library_id == "iec.breakers.mpcb_3p"
    assert da25.category == "protection.mpcb"
    assert da25.contact_config == "1NO1NC"
    assert da25.amps_min == 2.2
    assert da25.amps_max == 3.2
    assert da25.amps == 3.2
    assert da25.ka == 100
    assert "spring" in da25.description.lower()
    assert "1NO" in da25.description or "aux" in da25.description.lower()
    ba10 = parts.get("3RV2011-1BA10", "Siemens")
    assert ba10.library_id == "iec.breakers.mpcb_3p"
    assert ba10.amps_min == 1.4
    assert ba10.amps_max == 2
    s = Session.create("MPCB")
    s.place("iec.breakers.mpcb_3p", ref="Q1", x=20, y=20)
    s.assign_part("Q1", "3RV2011-1DA25", manufacturer="Siemens")
    props = s.sheet().component("Q1").properties
    assert props["range"] == "2.2-3.2A"
    assert props["amps_min"] == 2.2
    assert props["amps_max"] == 3.2
    assert props["In"] == "3.2A"
    assert props["contact_config"] == "1NO1NC"


def test_write_parts_csv(tmp_path: Path):
    dest = tmp_path / "parts.csv"
    parts = default_parts_catalog()
    write_parts_csv(dest, parts.parts.values())
    text = dest.read_text(encoding="utf-8-sig")
    assert "source" in text
    assert "verified" in text
    assert "1489-M3C160" in text
    assert "https://" in text
