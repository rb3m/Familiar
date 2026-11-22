from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from familiar_system.readers import LinuxReader, Load, Memory, PsutilReader
from familiar_system.sensors import match_rule, reported, warning_line


def hwmon(root, index, device, driver, label, value, maximum=None, critical=None):
    path = root / "class/hwmon" / f"hwmon{index}"
    path.mkdir(parents=True)
    target = root / "devices" / device
    target.mkdir(parents=True, exist_ok=True)
    (path / "device").symlink_to(target)
    (path / "name").write_text(driver)
    (path / "temp1_label").write_text(label)
    (path / "temp1_input").write_text(str(value))
    limits = {"max": maximum, "crit": critical}
    for suffix, limit in limits.items():
        write_limit(path, suffix, limit)
    return path, target


def write_limit(path, suffix, value):
    if value is not None:
        (path / f"temp1_{suffix}").write_text(str(value))


def test_sysfs_identity_sentinels_and_closed_inventory(tmp_path, settings):
    first, target = hwmon(tmp_path, 7, "nvme1", "nvme", "Composite", 76000, 75000, 65261850)
    (target / "model").write_text("Moonberry Mock Drive")
    hwmon(tmp_path, 4, "nvme2", "nvme", "Composite", 66000, 65261850, 85000)
    hwmon(tmp_path, 2, "mockcpu", "coretemp", "Package id 0", 54000, 100000)
    hwmon(tmp_path, 1, "mockram", "spd5118", "temp1", 0)
    hwmon(tmp_path, 5, "mockram2", "spd5118", "temp1", 127000)
    hwmon(tmp_path, 6, "mockchip", "unknown", "board", 40000)
    reader = LinuxReader(settings, sysfs_root=tmp_path)
    readings = reader.temperatures()
    drives = [sensor for sensor in readings if sensor.driver == "nvme"]
    assert len({sensor.id for sensor in drives}) == 2
    assert all("hwmon" not in sensor.id for sensor in readings)
    drive = next(sensor for sensor in drives if sensor.device.startswith("nvme1"))
    assert drive.device == "nvme1 (Moonberry Mock Drive)"
    assert drive.max_c == 75
    assert drive.crit_c is None
    assert len(reported(readings, settings)) == 3
    (first / "temp1_input").write_text("oops")
    assert reader._sensor(first, first / "temp1_input").celsius is None
    (first / "temp1_label").unlink()
    assert reader._sensor(first, first / "temp1_input").label == "temp1"


def partition(device, mountpoint, fstype):
    return SimpleNamespace(device=device, mountpoint=mountpoint, fstype=fstype)


def test_local_dedupe_and_network_never_probed(settings):
    backend = Mock()
    backend.disk_partitions.return_value = [
        partition("/dev/mockpool", "/long/path", "btrfs"),
        partition("/dev/mockpool", "/pool", "btrfs"),
        partition("//mockhost/share", "/network", "cifs"),
        partition("/dev/mockdisk", "/other", "ext4"),
    ]
    backend.disk_usage.return_value = SimpleNamespace(total=1000, free=80)
    reader = PsutilReader(settings, backend=backend)
    result = reader.filesystems()
    assert {fs.mountpoint for fs in result} == {"/pool", "/other"}
    assert result[0].free_pct == 8
    backend.disk_partitions.assert_called_once_with(all=False)
    assert {call.args[0] for call in backend.disk_usage.call_args_list} == {"/pool", "/other"}


def test_unavailable_resources_and_macos_temperatures(settings):
    backend = Mock()
    backend.disk_partitions.return_value = []
    backend.virtual_memory.side_effect = OSError
    backend.getloadavg.side_effect = OSError
    backend.cpu_count.return_value = None
    backend.boot_time.side_effect = OSError
    reader = PsutilReader(settings, backend=backend)
    result = reader.resources()
    assert reader.temperatures() == []
    assert result.memory == Memory()
    assert result.load == Load()
    assert result.uptime_s is None


def test_resources_values(settings, monkeypatch):
    backend = Mock()
    backend.disk_partitions.return_value = []
    backend.virtual_memory.return_value = SimpleNamespace(total=1000, available=200)
    backend.getloadavg.return_value = (0.5, 0.4, 0.3)
    backend.cpu_count.return_value = 8
    backend.boot_time.return_value = 100
    monkeypatch.setattr("familiar_system.readers.time.time", lambda: 150)
    result = PsutilReader(settings, backend=backend).resources()
    assert result.memory == Memory(1000, 200)
    assert result.load == Load(0.5, 0.4, 0.3, 8)
    assert result.uptime_s == 50


@pytest.mark.parametrize(
    "maximum,critical,rule,expected",
    [
        (75, 90, {"limit": "max"}, (75, "device max")),
        (75, 110, {"limit": "crit", "margin_c": 15}, (95, "device crit - margin")),
        (None, None, {"limit": "max", "fallback_c": 70}, (70, "fallback")),
        (None, None, {"limit": "max"}, (None, None)),
        (75, 90, {"alert": False}, (None, None)),
    ],
)
def test_warning_lines(maximum, critical, rule, expected):
    from familiar_system.readers import Temperature

    sensor = Temperature(max_c=maximum, crit_c=critical)
    assert warning_line(sensor, {"alert": True, **rule}) == expected


def test_rule_regex_exact_and_first_match(settings):
    from familiar_system.readers import Temperature

    sensor = Temperature(driver="coretemp", label="Package id 12")
    assert match_rule(sensor, settings)["role"] == "cpu"
    assert match_rule(Temperature(driver="coretemp", label="Core 0"), settings) is None
    settings["rules"].insert(0, {"driver": "coretemp", "label": "re:.*", "role": "first"})
    assert match_rule(sensor, settings)["role"] == "first"
