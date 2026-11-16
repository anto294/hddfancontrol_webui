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


def test_chip_and_locate(fake_sys: Path, fake_dev: Path) -> None:
    """A PWM file is found again on its chip after the hwmon number changed."""
    hardware = Hardware(fake_dev, fake_sys)
    chip = "/sys/devices/platform/nct6775.656"
    assert hardware.chip_of("/sys/class/hwmon/hwmon2/pwm1") == chip
    assert hardware.chip_of("/sys/class/hwmon/hwmon10/temp1_input") is None  # no device link
    assert hardware.locate("/sys/class/hwmon/hwmon2/pwm1", chip) == "/sys/class/hwmon/hwmon2/pwm1"
    (fake_sys / "class/hwmon/hwmon2").rename(fake_sys / "class/hwmon/hwmon7")
    assert hardware.locate("/sys/class/hwmon/hwmon2/pwm1", chip) == "/sys/class/hwmon/hwmon7/pwm1"
    assert hardware.locate("/sys/class/hwmon/hwmon2/pwm9", chip) is None
    assert hardware.locate("/sys/class/hwmon/hwmon2/pwm1", "/sys/devices/platform/it87.552") is None
    (fake_sys / "class/hwmon/hwmon7").rename(fake_sys / "class/hwmon/hwmon2")
    assert Hardware(fake_dev, fake_sys / "missing").locate("/sys/class/hwmon/hwmon2/pwm1", chip) is None


def test_drive_connection(tmp_path: Path) -> None:
    """The bus, the controller driver and the NVMe link come from the place of the drive in sysfs."""
    sys_root = tmp_path / "sys"
    dev_root = tmp_path / "dev"
    by_id = dev_root / "disk" / "by-id"
    by_id.mkdir(parents=True)
    drivers = sys_root / "bus" / "pci" / "drivers"
    places = {
        "sda": ("0000:00:17.0", "ahci", "ata3/host2/target2:0:0/2:0:0:0/block/sda"),
        "sdb": ("0000:01:00.0", "mpt3sas", "host0/port-0:0/end_device-0:0/target0:0:0/0:0:0:0/block/sdb"),
        "sdc": ("0000:00:14.0", "xhci_hcd", "usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0/block/sdc"),
        "nvme0n1": ("0000:02:00.0", "nvme", "nvme/nvme0/nvme0n1"),
    }
    for name, (address, driver, rest) in places.items():
        pci = sys_root / "devices" / "pci0000:00" / address
        place = pci / rest
        place.mkdir(parents=True)
        (drivers / driver).mkdir(parents=True, exist_ok=True)
        (pci / "driver").symlink_to(drivers / driver)
        (sys_root / "class" / "block").mkdir(parents=True, exist_ok=True)
        (sys_root / "class" / "block" / name).symlink_to(place)
        (dev_root / name).write_text("")
        (by_id / f"id-{name}").symlink_to(f"../../{name}")
    nvme = sys_root / "devices" / "pci0000:00" / "0000:02:00.0"
    (nvme / "current_link_speed").write_text("8.0 GT/s PCIe\n")
    (nvme / "current_link_width").write_text("4\n")
    (nvme / "nvme" / "nvme0" / "nvme0n1" / "device").symlink_to(nvme / "nvme" / "nvme0")
    (nvme / "nvme" / "nvme0" / "device").symlink_to(nvme)
    drives = {d.name: (d.bus, d.controller, d.link) for d in Hardware(dev_root, sys_root).drives()}
    assert drives == {
        "sda": ("sata", "ahci", ""),
        "sdb": ("sas", "mpt3sas", ""),
        "sdc": ("usb", "xhci_hcd", ""),
        "nvme0n1": ("nvme", "nvme", "8.0 GT/s PCIe x4"),
    }
