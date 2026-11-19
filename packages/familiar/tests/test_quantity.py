import pytest
from familiar.interfaces import spoken


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (6.5531, "ly", "6.6 light-years"),
        (1, "ly", "1 light-year"),
        (1234.5, "m/s", "1,235 metres per second"),
        (9.876, "m", "9.9 metres"),
        (12.34, "km", "12.3 kilometres"),
        (285, "s", "4 minutes 45 seconds"),
        (3605, "s", "1 hour 5 seconds"),
        (None, "ly", "unknown"),
        (0, "m", "0 metres"),
        (0.012345, "m", "0.012 metres"),
        (-6.5531, "ly", "-6.6 light-years"),
        (10, "km", "10 kilometres"),
        (99.94, "km", "99.9 kilometres"),
        (100.4, "km", "100 kilometres"),
        (100.5, "km", "101 kilometres"),
        (0.999, "m", "1 metre"),
        (1, "m/s", "1 metre per second"),
        (2, "cr", "2 credits"),
        (1, "cr", "1 credit"),
        (0, "s", "0 seconds"),
        (1, "s", "1 second"),
        (59.5, "s", "1 minute"),
        (61, "s", "1 minute 1 second"),
        (3600, "s", "1 hour"),
        (3661, "s", "1 hour 1 minute 1 second"),
        (7200, "s", "2 hours"),
        (-61, "s", "-1 minute 1 second"),
    ],
)
def test_spoken(value, unit, expected):
    assert spoken(value, unit) == expected


def test_unknown_unit_raises():
    with pytest.raises(ValueError, match="Unknown unit"):
        spoken(2, "furlong")


@pytest.mark.parametrize("value", [2, None])
def test_unknown_unit_even_when_value_is_missing(value):
    with pytest.raises(ValueError, match="Unknown unit"):
        spoken(value, "typo")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_quantity(value):
    with pytest.raises(ValueError, match="finite"):
        spoken(value, "m")


def test_unit_overrides_reload(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path))
    package = tmp_path / "familiar"
    package.mkdir()
    override = package / "units.yaml"
    override.write_text("ly: [custom year, custom years]\ns: [tick, ticks]\n")
    assert spoken(1, "ly") == "1 custom year"
    assert spoken(61, "s") == "1 minute 1 tick"
    override.write_text("ly: [changed year, changed years]\n")
    assert spoken(2, "ly") == "2 changed years"
