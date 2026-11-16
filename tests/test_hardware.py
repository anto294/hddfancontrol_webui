"""Drives and hwmon devices read from a fake /sys and /dev."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hddfancontrol_webui.hardware import Hardware


def test_drives(fake_sys: Path, fake_dev: Path) -> None:
    """Whole drives only, grouped by kernel name, model names before wwn/eui names."""
    drives = Hardware(fake_dev, fake_sys).drives()
    assert [d.name for d in drives] == ["nvme0n1", "sda"]
    sda = drives[1]
    assert sda.ids == [
        "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW",
        "/dev/disk/by-id/wwn-0x5000c500e0000001",
    ]
    assert sda.model == "ST4000VN006"
    assert sda.size_bytes == 7814037168 * 512
    assert sda.rotational is True
    assert drives[0].ids[0] == "/dev/disk/by-id/nvme-Samsung_980_S64"
    assert drives[0].rotational is False


def test_hwmons(fake_sys: Path, fake_dev: Path) -> None:
    """PWM outputs with their fan speed, temperatures in °C, natural order."""
    hardware = Hardware(fake_dev, fake_sys)
    devices = hardware.hwmons()
    assert [d.hwmon for d in devices] == ["hwmon2", "hwmon10"]
    chip = devices[0]
    assert chip.name == "nct6798"
    assert [(p.path, p.value, p.enable, p.rpm) for p in chip.pwms] == [
        ("/sys/class/hwmon/hwmon2/pwm1", 128, 5, 812),
        ("/sys/class/hwmon/hwmon2/pwm2", 255, 1, None),
    ]
    assert [(t.label, t.celsius) for t in chip.temps] == [("SYSTIN", 41.5), ("temp2", 38.0)]
    assert devices[1].pwms == []
    assert hardware.pwm_paths() == {"/sys/class/hwmon/hwmon2/pwm1", "/sys/class/hwmon/hwmon2/pwm2"}
    snapshot = hardware.snapshot()
    assert snapshot["drives"][1]["name"] == "sda"
    assert snapshot["hwmons"][0]["pwms"][0]["rpm"] == 812


def test_missing_trees(tmp_path: Path) -> None:
    """A host without hwmon or by-id gives empty lists."""
    hardware = Hardware(tmp_path / "dev", tmp_path / "sys")
    assert hardware.drives() == []
    assert hardware.hwmons() == []


def test_unreadable_values(fake_sys: Path, fake_dev: Path) -> None:
    """Garbage in a sysfs file reads as unknown."""
    (fake_sys / "class" / "hwmon" / "hwmon2" / "fan1_input").write_text("n/a\n")
    pwm = Hardware(fake_dev, fake_sys).hwmons()[0].pwms[0]
    assert pwm.rpm is None


def test_read_only(fake_sys: Path, fake_dev: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A writable /sys is not read only; a read-only mount or a missing tree is."""
    assert Hardware(fake_dev, fake_sys).read_only() is False
    assert Hardware(fake_dev, tmp_path / "missing").read_only() is True

    class Mount:
        f_flag = os.ST_RDONLY

    monkeypatch.setattr(os, "statvfs", lambda _: Mount())
    assert Hardware(fake_dev, fake_sys).read_only() is True
