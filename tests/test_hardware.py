"""Drives and hwmon devices read from a fake /sys and /dev."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hddfancontrol_webui.hardware import DriveIo, Hardware, HwmonDevice, TempInput


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


def test_kernel_names(fake_sys: Path, fake_dev: Path) -> None:
    """Stable names of whole drives and /dev/sdX lead to the kernel name; partitions do not count."""
    names = Hardware(fake_dev, fake_sys).kernel_names()
    assert names["/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"] == "sda"
    assert names["/dev/sda"] == "sda" and names["/dev/disk/by-id/nvme-eui.002538"] == "nvme0n1"
    assert not any("part" in key for key in names)
    assert Hardware(fake_dev / "none", fake_sys).kernel_names() == {}


def test_drive_io(fake_sys: Path, fake_dev: Path) -> None:
    """The I/O counters of a drive come from its stat file; a missing or bad file gives none."""
    block = fake_sys / "class" / "block"
    (block / "sda" / "stat").write_text("  4012 10 821344 900 2210 5 409600 3100 2 15230 4000 0 0 0 0 0 0\n")
    drives = {d.name: d for d in Hardware(fake_dev, fake_sys).drives()}
    assert drives["sda"].io == DriveIo(busy_ms=15230, read_sectors=821344, write_sectors=409600, in_flight=2)
    assert drives["nvme0n1"].io is None
    (block / "sda" / "stat").write_text("1 2 3\n")
    assert Hardware(fake_dev, fake_sys).drives()[1].io is None
    (block / "sda" / "stat").write_text("a b c d e f g h i j\n")
    assert Hardware(fake_dev, fake_sys).drives()[1].io is None


def test_cpu(fake_sys: Path, fake_dev: Path, tmp_path: Path) -> None:
    """Time counters and processors from /proc/stat, the load average, the hottest package."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(
        "cpu  100 5 50 800 20 3 2 0 7 0\ncpu0 50 2 25 400 10 1 1 0 0 0\ncpu1 50 3 25 400 10 2 1 0 0 0\nintr 1 2 3\n"
    )
    (proc / "loadavg").write_text("0.52 0.40 0.31 2/301 4242\n")
    cpu = Hardware(fake_dev, fake_sys, proc).cpu()
    assert (cpu.count, cpu.total, cpu.busy, cpu.load) == (2, 980, 160, [0.52, 0.40, 0.31])
    # The fake host has a fan chip and acpitz only: no processor temperature.
    assert (cpu.temp, cpu.temp_path) == (None, "")
    snapshot = Hardware(fake_dev, fake_sys, proc).snapshot()
    assert snapshot["cpu"]["busy"] == 160 and isinstance(snapshot["clock"], float)

    assert Hardware(fake_dev, fake_sys, proc).cpu_temp() is None
    chip = fake_sys / "class" / "hwmon" / "hwmon7"
    chip.mkdir()
    (chip / "name").write_text("coretemp\n")
    (chip / "temp1_label").write_text("Package id 0\n")
    (chip / "temp1_input").write_text("52000\n")
    (chip / "temp2_label").write_text("Core 0\n")
    (chip / "temp2_input").write_text("70000\n")
    assert Hardware(fake_dev, fake_sys, proc).cpu_temp() == 52.0
    assert Hardware(fake_dev, tmp_path / "nothing", proc).cpu_temp() is None

    empty = Hardware(fake_dev, fake_sys, tmp_path / "none").cpu()
    assert (empty.count, empty.total, empty.busy, empty.load) == (0, None, None, [])
    (proc / "stat").write_text("cpu  1 x 3\n")
    (proc / "loadavg").write_text("busy\n")
    bad = Hardware(fake_dev, fake_sys, proc).cpu()
    assert (bad.total, bad.load) == (None, [])


def test_cpu_temperature() -> None:
    """The package input of each processor chip, the hottest of them; Tdie before Tctl."""

    def chip(name: str, *temps: tuple[str, float | None]) -> HwmonDevice:
        number = len(name)
        inputs = [
            TempInput(f"/sys/class/hwmon/hwmon{number}/temp{i}_input", label, value)
            for i, (label, value) in enumerate(temps, 1)
        ]
        return HwmonDevice(hwmon=f"hwmon{number}", name=name, temps=inputs)

    pick = Hardware._cpu_temp
    intel = chip("coretemp", ("Core 0", 70.0), ("Package id 0", 55.0))
    assert pick([intel]) == (55.0, "/sys/class/hwmon/hwmon8/temp2_input")
    two = chip("coretemp", ("Package id 0", 51.0), ("Package id 1", 58.0))
    assert pick([two])[0] == 58.0
    amd = chip("k10temp", ("Tctl", 72.0), ("Tdie", 62.0), ("Tccd1", 64.0))
    assert pick([amd])[0] == 62.0
    assert pick([chip("k10temp", ("Tctl", 48.0), ("Tccd1", 50.0))])[0] == 48.0
    assert pick([chip("cpu_thermal", ("temp1", 47.2))])[0] == 47.2
    assert pick([chip("coretemp", ("Core 0", None), ("Core 1", 44.0))])[0] == 44.0
    assert pick([chip("acpitz", ("temp1", 30.0)), chip("nct6798", ("CPUTIN", 40.0))]) == (None, "")


def test_report(fake_sys: Path, fake_dev: Path, tmp_path: Path) -> None:
    """The support report: board names, chips with their files, the usual modules, drives; never a serial."""
    dmi = fake_sys / "class" / "dmi" / "id"
    dmi.mkdir(parents=True)
    for name, value in {
        "sys_vendor": "Dell Inc.",
        "product_name": "PowerEdge R730xd",
        "product_serial": "SECRET1",
        "board_serial": "SECRET2",
    }.items():
        (dmi / name).write_text(value + "\n")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "modules").write_text("nct6775 45056 0 - Live 0x0\nzfs 6000000 1 - Live 0x0\nipmi_si 90000 0 - Live 0x0\n")
    report = Hardware(fake_dev, fake_sys, proc).report()
    assert "SECRET" not in report
    assert "sys_vendor: Dell Inc.\nproduct_name: PowerEdge R730xd\nboard_vendor: \n" in report
    assert "== modules\nnct6775 ipmi_si\n" in report
    assert "pwm1" in report and "== drives\n" in report and report.endswith("== bmc\nno /dev/ipmi0")
    (fake_dev / "ipmi0").write_text("")
    (proc / "modules").write_text("zfs 1 0\n")
    report = Hardware(fake_dev, tmp_path / "nothing", proc).report()
    assert "== hwmon\n== modules\nnone of the usual ones" in report and report.endswith("/dev/ipmi0")
