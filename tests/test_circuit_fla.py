"""NEC Table 430.250 FLA lookup used by Circuit Builder."""

from panelcad.core.circuit_fla import delta_ol_setpoint, lookup_fla


def test_table_430_250_known_rows():
    assert lookup_fla(hp=10, voltage=460) == 14.0
    assert lookup_fla(hp=10, voltage=230) == 28.0
    assert lookup_fla(hp=1.5, voltage=460) == 3.0
    assert lookup_fla(kw=7.46, voltage=460) == 14.0  # 10 hp
    assert delta_ol_setpoint(21.0) == 12.18


def test_lookup_none_without_size():
    assert lookup_fla(voltage=480) is None
