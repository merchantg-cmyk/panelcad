"""Published UL 508A Supplement SB numbers used by the SCCR calculator.

Every value below is copied from a manufacturer guide that reprints the
table. Cells those guides do not agree on are marked and the higher peak
let-through is stored, which cannot raise a panel rating above both sources.
Rows that could not be read are omitted; the calculator then asks for a
user value instead of inventing one.

Table SB4.1 (assumed maximum SCCR for unmarked components), kA.
Agreeing reprints:
- Mersen Tech Topic TT-SCN1, Table SB4.1 effective April 25, 2006
  https://us.mersen.com/sites/default/files/files_imported_ep/TT-SCN1-Achieving-Higher-Short-Circuit-Current-Ratings-for-Industrial-Control-Panels-Tech-Topic.pdf
- Schneider Electric Control Panel Technical Guide, CPTG005_EN (06-2017), annex Table SB4.1
  https://cedorlando.com/wp-content/uploads/Control-Panel-Technical-Guide.pdf
Eaton's 2008 SPD reprint differs at two cells (401–600 hp printed as 20 kA,
and a mercury-switch line that does not match the other two). Those Eaton
cells are not used.
https://www.eaton.com/content/dam/eaton/products/electrical-circuit-protection/fuses/solution-center/bus-ele-tech-lib-short-circuit-current-rating-calculations-and-marking-2008-spd.pdf

Table SB4.2 peak let-through Ip (kA) at 50 / 100 / 200 kA available.
Same Mersen reprint (table effective April 25, 2006) and Eaton SPD Table SCCR2.
Eaton notes that some intermediate 100 kA Class J/T rows are UL 248 umbrella
limits and were not yet in Supplement SB. Those intermediate rows are not
stored. An ampere rating between two stored rows uses the next higher row.
"""

from __future__ import annotations

from dataclasses import dataclass

# Motor-controller standard-fault bands. hp_hi is inclusive.
# kW column is the parenthetical range printed beside the horsepower band.
MOTOR_CONTROLLER_BANDS: tuple[tuple[float, float, float, float, float, str], ...] = (
    (0.0, 50.0, 0.0, 37.3, 5.0, "0–50 hp (0–37.3 kW)"),
    (51.0, 200.0, 38.0, 149.0, 10.0, "51–200 hp (38–149 kW)"),
    (201.0, 400.0, 150.0, 298.0, 18.0, "201–400 hp (150–298 kW)"),
    (401.0, 600.0, 299.0, 447.0, 30.0, "401–600 hp (299–447 kW)"),
    (601.0, 900.0, 448.0, 671.0, 42.0, "601–900 hp (448–671 kW)"),
    (901.0, 1500.0, 672.0, 1193.0, 85.0, "901–1500 hp (672–1193 kW)"),
)

# Unmarked-component defaults that two reprints agree on.
SB41_KA: dict[str, float] = {
    "bus_bars": 10.0,
    "circuit_breaker": 5.0,
    "current_shunt": 10.0,
    "fuse_holder": 10.0,
    "overload_relay": 5.0,
    "switch": 5.0,
    "mercury_over_60a_or_250v": 5.0,
    "mercury_250v_60a_over_2kva": 3.5,
    "mercury_250v_2kva_or_less": 1.0,
    "meter_socket": 10.0,
    "miniature_fuse": 10.0,
    "receptacle_gfci": 2.0,
    "receptacle": 10.0,
    "supplementary_protector": 0.2,
    "switch_unit": 5.0,
    "terminal_or_pdb": 10.0,
}

# Peak let-through Ip in kA at the 50, 100, and 200 kA columns.
# None means the reprinted table has no value in that column (a dash).
# Class J and 600 V Class T share one block in both reprints.
FUSE_IP_KA: dict[tuple[str, int], tuple[float | None, float | None, float | None]] = {
    ("CC", 15): (3.0, 3.0, 4.0),
    ("CC", 20): (3.0, 4.0, 5.0),
    ("CC", 30): (6.0, 7.5, 12.0),
    ("G", 15): (None, 4.0, None),
    ("G", 20): (None, 5.0, None),
    ("G", 30): (None, 7.0, None),
    ("G", 60): (None, 10.5, None),
    ("J", 30): (6.0, 7.5, 12.0),
    ("J", 60): (8.0, 10.0, 16.0),
    ("J", 100): (12.0, 14.0, 20.0),
    ("J", 200): (16.0, 20.0, 30.0),
    ("J", 400): (25.0, 30.0, 45.0),
    ("J", 600): (35.0, 45.0, 70.0),
    ("J", 800): (50.0, 55.0, 75.0),
    ("T300", 30): (5.0, 7.0, 9.0),
    ("T300", 60): (7.0, 9.0, 12.0),
    ("T300", 100): (9.0, 12.0, 15.0),
    ("T300", 200): (13.0, 16.0, 20.0),
    ("T300", 400): (22.0, 28.0, 35.0),
    ("T300", 600): (29.0, 37.0, 46.0),
    ("T300", 800): (37.0, 50.0, 65.0),
    ("T300", 1200): (50.0, 65.0, 80.0),
    ("RK1", 30): (6.0, 10.0, 12.0),
    ("RK1", 60): (10.0, 12.0, 16.0),
    ("RK1", 100): (14.0, 16.0, 20.0),
    ("RK1", 200): (18.0, 22.0, 30.0),
    ("RK1", 400): (33.0, 35.0, 50.0),
    ("RK1", 600): (45.0, 50.0, 70.0),
    ("RK5", 30): (11.0, 11.0, 14.0),
    ("RK5", 60): (20.0, 21.0, 26.0),
    ("RK5", 100): (22.0, 25.0, 32.0),
    ("RK5", 200): (32.0, 40.0, 50.0),
    ("RK5", 400): (50.0, 60.0, 75.0),
    ("RK5", 600): (65.0, 80.0, 100.0),
    ("L", 800): (80.0, 80.0, 80.0),
    ("L", 1200): (80.0, 80.0, 120.0),
    ("L", 1600): (100.0, 100.0, 150.0),
    ("L", 2000): (110.0, 120.0, 165.0),
    ("L", 2500): (None, 165.0, 180.0),
    ("L", 3000): (None, 175.0, 200.0),
    ("L", 4000): (None, 220.0, 250.0),
    ("L", 5000): (None, None, 300.0),
    ("L", 6000): (None, None, 350.0),
}

# Column index 0/1/2 = 50/100/200 kA. Higher Ip is stored where reprints differ.
FUSE_IP_CONFLICTS: dict[tuple[str, int, int], str] = {
    ("T300", 100, 2): (
        "300 V Class T, 100 A, 200 kA column: Mersen prints Ip 12 kA and Eaton prints 15 kA. "
        "15 kA is used."
    ),
    ("RK1", 600, 0): (
        "Class RK1, 600 A, 50 kA column: Mersen prints Ip 43 kA and Eaton prints 45 kA. "
        "45 kA is used."
    ),
}

FUSE_COLUMNS_KA: tuple[int, int, int] = (50, 100, 200)

CURRENT_LIMITING_FUSE_CLASSES = frozenset({"CC", "G", "J", "L", "RK1", "RK5", "T", "T300", "T600"})


@dataclass(frozen=True)
class Citation:
    id: str
    section: str
    what: str
    url: str
    used_for: str


CITATIONS: tuple[Citation, ...] = (
    Citation(
        "sb4.1",
        "UL 508A Supplement SB, Table SB4.1 (reprinted; table effective April 25, 2006 in the Mersen copy)",
        "Assumed maximum short-circuit current rating for unmarked components",
        "https://us.mersen.com/sites/default/files/files_imported_ep/TT-SCN1-Achieving-Higher-Short-Circuit-Current-Ratings-for-Industrial-Control-Panels-Tech-Topic.pdf",
        "Default SCCR when a power-circuit part has no marked high-fault rating",
    ),
    Citation(
        "sb4.1-schneider",
        "Schneider Control Panel Technical Guide, annex Table SB4.1",
        "Same Table SB4.1 defaults, including 30 kA for the 401–600 hp band and 5 kA for a switch unit",
        "https://cedorlando.com/wp-content/uploads/Control-Panel-Technical-Guide.pdf",
        "Second source for every Table SB4.1 value that is hard-coded",
    ),
    Citation(
        "sb4.2-method",
        "UL 508A SB4.2 / SB4.2.1 / SB4.2.2, as summarized by Rockwell Automation SCCR-AT002",
        "Individual power-circuit component SCCR from a marking, Table SB4.1, or a tested combination. Some devices (voltmeters, resistors, and the devices Siemens lists) do not require an SCCR.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Where a component rating comes from, and which devices are in the power circuit",
    ),
    Citation(
        "sb4.2.3",
        "UL 508A SB4.2.3, Rockwell SCCR-AT002 paragraph SB4.2.3",
        "A high-fault SCCR is used only with the specified fuse or breaker, with the four listed substitution exceptions.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Catalog string SCCR is flagged until the specified protective device is identified",
    ),
    Citation(
        "sb4.2-feeder",
        "UL 508A SB4.4.2 and the feeder-circuit definition in Rockwell SCCR-AT002; Eaton SPD two-sweep method",
        "Feeder components are on the supply side of the branch overcurrent device. Their rating is not raised by a downstream branch device. The panel rating is the lowest unmodified feeder rating, unmodified branch rating, or SB4.3 modified rating.",
        "https://www.eaton.com/content/dam/eaton/products/electrical-circuit-protection/fuses/solution-center/bus-ele-tech-lib-short-circuit-current-rating-calculations-and-marking-2008-spd.pdf",
        "Feeder-component limitation",
    ),
    Citation(
        "sb4.3.3",
        "UL 508A SB4.3.3, Rockwell SCCR-AT002; Siemens UL SCCR white paper (February 2018)",
        "Class CC, G, J, L, RK1, RK5, and T fuses in the feeder may limit let-through. Peak current is taken from Table SB4.2, not from a fuse manufacturer's curve. Branch-device interrupting ratings are not series-rated upward.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Fuse class and ampere peak let-through",
    ),
    Citation(
        "sb4.2-ip",
        "UL 508A Table SB4.2, Mersen TT-SCN1 and Eaton SPD Table SCCR2",
        "Peak let-through current Ip and clearing I2t. This calculator uses Ip. I2t is not applied.",
        "https://us.mersen.com/sites/default/files/files_imported_ep/TT-SCN1-Achieving-Higher-Short-Circuit-Current-Ratings-for-Industrial-Control-Panels-Tech-Topic.pdf",
        "Numeric Ip values",
    ),
    Citation(
        "sb4.3-compare",
        "Mersen TT-SCN1 step 2 and Siemens white paper, current limitation by fuses",
        "The published comparison is numeric: component SCCR in kA rms is not less than the Table SB4.2 peak let-through in kA. A 10 kA component is suitable when Ip is 10 kA.",
        "https://assets.new.siemens.com/siemens/assets/api/uuid:e38c994f-b2da-4846-ba00-f8e4733dfbde/ulsccr-whitepaper-icp-us.pdf",
        "Ip compared directly with the component SCCR",
    ),
    Citation(
        "sb4.3.2",
        "UL 508A SB4.3.2, Rockwell SCCR-AT002; Siemens white paper",
        "A UL Listed breaker marked current-limiting may be used only with the manufacturer's published let-through. UL 489 has no umbrella table comparable to Table SB4.2.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Current-limiting breakers require a user-entered let-through",
    ),
    Citation(
        "series-rating",
        "Eaton SPD, Determining Assembly SCCR",
        "An overcurrent-device interrupting rating is not raised by an upstream overcurrent device. Series ratings are not used for an industrial-control-panel SCCR.",
        "https://www.eaton.com/content/dam/eaton/products/electrical-circuit-protection/fuses/solution-center/bus-ele-tech-lib-short-circuit-current-rating-calculations-and-marking-2008-spd.pdf",
        "Branch and feeder breaker or fuse interrupting ratings stay at their marked value",
    ),
    Citation(
        "sb4.3.1",
        "UL 508A SB4.3.1, Rockwell SCCR-AT002 (UL 508A 3rd edition, April 24, 2018)",
        "Isolated-secondary transformer. IFL = kVA×1000/V for single-phase and kVA×1000/(V×√3) for three-phase. Unmarked impedance, or impedance below 2.1%, is taken as 2.1%. If every secondary component and overcurrent device is rated at least the calculated secondary current, the primary overcurrent device's interrupting rating is the line-side rating of that circuit.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Transformer secondary current and the line-side rating",
    ),
    Citation(
        "sb4.3.1-z",
        "Siemens UL SCCR white paper, February 2018, Current limitation by transformers",
        "Same formulas. Z = 2.1% when impedance is unknown. Tables SB4.3 and SB4.4 are the alternate lookup; their grids are not encoded here.",
        "https://assets.new.siemens.com/siemens/assets/api/uuid:e38c994f-b2da-4846-ba00-f8e4733dfbde/ulsccr-whitepaper-icp-us.pdf",
        "2.1% impedance default; formula in place of Tables SB4.3 and SB4.4",
    ),
    Citation(
        "sb4.3.1-10kva",
        "Eaton SPD step 3 and Schneider CPTG 'Special case of transformer circuits (UL 508A chap. SB4.3.1)'",
        "When secondary voltage is not known, so ISC cannot be calculated: a transformer of 10 kVA or less may take the primary device rating if every secondary component and overcurrent device is at least 5 kA. A transformer of 5 kVA or less with a secondary of 120 V or less uses 2 kA instead of 5 kA. The worked examples show 10 kA only as the upstream device's own rating, not as a fixed transformer assignment.",
        "https://cedorlando.com/wp-content/uploads/Control-Panel-Technical-Guide.pdf",
        "10 kVA / 5 kA and 5 kVA / 2 kA screen used only when ISC cannot be calculated",
    ),
    Citation(
        "sb4.4",
        "UL 508A SB4.4.1 and SB4.4.2, Rockwell SCCR-AT002 step 3",
        "Branch rating is the smaller of the load-side power components and the branch protective device. The panel rating is the lowest branch, feeder, or SB4.3-modified value.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Panel SCCR is the lowest result",
    ),
    Citation(
        "exempt",
        "Siemens UL SCCR white paper, Components which are not relevant for SCCR consideration",
        "Power transformers, reactors, current transformers, dry-type capacitors, resistors, varistors, voltmeters, the wye-delta S contactor, cord-connected enclosure air conditioners, ferrules, conductors, and field loads (motors, heaters) are omitted. Control-circuit components are omitted. The primary overcurrent device of a control circuit still counts.",
        "https://assets.new.siemens.com/siemens/assets/api/uuid:e38c994f-b2da-4846-ba00-f8e4733dfbde/ulsccr-whitepaper-icp-us.pdf",
        "Motors, coils, operators, and the transformer body are not the weak link by themselves",
    ),
    Citation(
        "sb5.1",
        "UL 508A SB5.1, Rockwell SCCR-AT002 step 4; NEC 409.110",
        "The industrial-control-panel nameplate shows the SCCR and the maximum voltage.",
        "https://literature.rockwellautomation.com/idc/groups/literature/documents/at/sccr-at002_-en-p.pdf",
        "Nameplate line on the title block",
    ),
    Citation(
        "nec409.22",
        "NEC 409.22, as summarized in the Siemens white paper and Rockwell SCCR-AT002",
        "The panel is not connected to a supply whose available fault current exceeds the panel SCCR. The available current is the user's figure.",
        "https://assets.new.siemens.com/siemens/assets/api/uuid:e38c994f-b2da-4846-ba00-f8e4733dfbde/ulsccr-whitepaper-icp-us.pdf",
        "Design check when panel SCCR is below the entered available fault current",
    ),
)


def citation_by_id(citation_id: str) -> Citation:
    for item in CITATIONS:
        if item.id == citation_id:
            return item
    raise KeyError(citation_id)


def citation_markdown() -> str:
    """Citation table written next to the sample report pages."""

    lines = [
        "# UL 508A Supplement SB citations",
        "",
        "PanelCAD does not ship the UL standard. Each hard-coded rule or table value",
        "below is taken from a published reprint or application note. The UL standard",
        "remains the document an inspector applies.",
        "",
        "Not hard-coded, and flagged for the user to enter:",
        "",
        "- Table SB4.3 and Table SB4.4 numeric grids (secondary transformer current). The SB4.3.1 formula with Z = 2.1% is used instead, or `transformer_isc_ka`.",
        "- Fuse interrupting rating, unless the part or the user supplies `ir_ka`.",
        "- Peak let-through of a current-limiting circuit breaker (`let_through_ka` at `let_through_at_ka`).",
        "- Horsepower above 1500 hp (672–1193 kW is the last printed band).",
        "- Directly connected current-meter SCCR (Table SB4.1 note: a marked rating is required).",
        "- Mercury-tube switch SCCR when volts, amperes, and kVA are not all known.",
        "- Any component with no Table SB4.1 row and no entered SCCR.",
        "- Available fault current at the panel (`available_fault_ka`).",
        "- Panel voltage when the drawing does not state one (the field is editable; 480 V is only a flagged stand-in).",
        "- Class T 300 V versus 600 V when voltage is omitted (the higher Ip of the two rows is used and flagged).",
        "- Class CF and any other fuse class outside Table SB4.2 (enter let_through_ka at let_through_at_ka).",
        "- Transformer secondary voltage when only a ceiling is known (secondary_voltage_ceiling). The 5 kA or 2 kA screen is used.",
        "- The c3controls reprint's 2 kA switch unit and its 901–1600 hp band, and Eaton's 20 kA print of the 401–600 hp band. Two other reprints agree on 5 kA and on 901–1500 hp at 85 kA / 401–600 hp at 30 kA, so those are the stored values.",
        "",
        "| Id | Section | What it supplies | URL |",
        "| --- | --- | --- | --- |",
    ]
    for item in CITATIONS:
        what = item.what.replace("|", "/")
        lines.append(f"| `{item.id}` | {item.section} | {what} | {item.url} |")
    lines.append("")
    return "\n".join(lines)
