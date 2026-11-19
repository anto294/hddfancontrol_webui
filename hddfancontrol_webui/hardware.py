"""What the host offers: drives, PWM outputs, fan speeds and temperature inputs.

Everything here reads sysfs and /dev only. Nothing sends a command to a
drive, so a drive that sleeps is never woken up by the web UI: the drive
temperatures come from the log that hddfancontrol writes (see templog.py).

The paths returned are always the real ones (/dev/..., /sys/...), the ones
hddfancontrol needs, even when the tests read a fake tree somewhere else.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_PART = re.compile(r"-part\d+$")
# What a drive is in /dev. The tests, whose /dev holds plain files, accept those too.
_IS_DRIVE_NODE = stat.S_ISBLK
# Several names point at the same drive. These say less than the model and
# serial names, and only show when a drive has no other one.
_LOW_PRIORITY = ("wwn-", "nvme-eui.", "nvme-nvme.", "eui.")
_PWM = re.compile(r"^pwm(\d+)$")
_TEMP = re.compile(r"^temp(\d+)_input$")
_HWMON = re.compile(r"^hwmon\d+$")
_ATA_PORT = re.compile(r"^ata\d+$")
_PCI_ADDRESS = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$")
# The hwmon drivers of processors, and the input of each that stands for the
# whole package (the first input when none of them is there).
_CPU_CHIPS = {
    "coretemp": ("Package id 0", "Package id 1", "Package id 2", "Package id 3"),
    "k10temp": ("Tdie", "Tctl"),
    "zenpower": ("Tdie", "Tctl"),
    "cpu_thermal": (),
    "cpu-thermal": (),
    "soc_thermal": (),
}


# ---------------------------
#   DriveIo
# ---------------------------
@dataclass
class DriveIo:
    """The I/O counters of a drive since boot, from /sys/class/block/NAME/stat.

    They are kernel counters: reading them never reaches the drive. The page
    turns two readings into a rate with the clock of the snapshot.
    """

    busy_ms: int
    read_sectors: int
    write_sectors: int
    in_flight: int


# ---------------------------
#   Drive
# ---------------------------
@dataclass
class Drive:
    """A whole drive and the stable names that point at it."""

    name: str
    ids: list[str] = field(default_factory=list)
    model: str = ""
    size_bytes: int = 0
    rotational: bool | None = None
    # How it is connected: "sata", "sas", "nvme" or "usb"; the kernel driver of
    # its controller (ahci, mpt3sas, nvme...); for NVMe, the PCIe link.
    bus: str = ""
    controller: str = ""
    link: str = ""
    io: DriveIo | None = None


# ---------------------------
#   Cpu
# ---------------------------
@dataclass
class Cpu:
    """The processors of the host: time counters, load average and temperature.

    busy and total are the jiffies of /proc/stat since boot (all processors);
    the page turns two readings into a percentage.
    """

    count: int = 0
    busy: int | None = None
    total: int | None = None
    load: list[float] = field(default_factory=list)
    temp: float | None = None
    temp_path: str = ""


# ---------------------------
#   PwmOutput
# ---------------------------
@dataclass
class PwmOutput:
    """A PWM file of a hwmon device, with the speed of the fan of the same number."""

    path: str
    value: int | None
    enable: int | None
    rpm: int | None


# ---------------------------
#   TempInput
# ---------------------------
@dataclass
class TempInput:
    """A temperature input of a hwmon device."""

    path: str
    label: str
    celsius: float | None


# ---------------------------
#   HwmonDevice
# ---------------------------
@dataclass
class HwmonDevice:
    """A hwmon device: a chip with PWM outputs, fans and temperature inputs."""

    hwmon: str
    name: str
    pwms: list[PwmOutput] = field(default_factory=list)
    temps: list[TempInput] = field(default_factory=list)


# ---------------------------
#   _read
# ---------------------------
def _read(path: Path) -> str | None:
    """Read a small sysfs file, or None when it is missing or unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


# ---------------------------
#   _read_int
# ---------------------------
def _read_int(path: Path) -> int | None:
    """Read an integer sysfs file, or None."""
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


# What the hardware support request asks for (.github/ISSUE_TEMPLATE/hardware_support.yml):
# the same lines its shell block prints. Names and model numbers only: the
# DMI fields are named one by one, so no *_serial or product_uuid is ever read.
_REPORT_DMI = ("sys_vendor", "product_name", "board_vendor", "board_name", "bios_version")
_REPORT_FILES = re.compile(r"^(pwm\d+|fan\d+_input|temp\d+_input)$")
_REPORT_MODULES = re.compile(r"^(nct|it87|f71|w83|asus|dell|hp|coretemp|k10temp|drivetemp|ipmi)", re.IGNORECASE)


# ---------------------------
#   _natural_key
# ---------------------------
def _natural_key(text: str) -> list[Any]:
    """Sort hwmon2 before hwmon10 and pwm2 before pwm10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


# ---------------------------
#   Hardware
# ---------------------------
class Hardware:
    """Read the drives and the hwmon devices of the host."""

    def __init__(
        self,
        dev_root: Path = Path("/dev"),
        sys_root: Path = Path("/sys"),
        proc_root: Path = Path("/proc"),
        bmc: Callable[[], HwmonDevice | None] | None = None,
    ) -> None:
        """Read from these trees: the real ones, or a copy in the tests.

        bmc (experimental, bmc.Bmc.hwmon) gives the BMC of a server as one
        more hwmon device, or None when there is none.
        """
        self._dev = dev_root
        self._sys = sys_root
        self._proc = proc_root
        self._bmc = bmc

    def report(self) -> str:
        """The host as a hardware support request asks for it: board, kernel, hwmon chips, modules, drives, BMC device.

        The same lines as the shell block of the issue form, so a report from
        the web UI and one pasted by hand read alike. Never a serial number.
        """
        dmi = self._sys / "class" / "dmi" / "id"
        lines = ["== board", *(f"{name}: {_read(dmi / name) or ''}" for name in _REPORT_DMI)]
        lines += ["== kernel", os.uname().release, "== hwmon"]
        base = self._sys / "class" / "hwmon"
        try:
            chips = sorted((p for p in base.iterdir() if _HWMON.match(p.name)), key=lambda p: _natural_key(p.name))
        except OSError:
            chips = []
        for chip in chips:
            try:
                files = sorted((p.name for p in chip.iterdir() if _REPORT_FILES.match(p.name)), key=_natural_key)
            except OSError:
                files = []
            lines.append(f"{chip.name} {_read(chip / 'name') or ''}: {' '.join(files)}")
        modules = [line.split()[0] for line in (_read(self._proc / "modules") or "").splitlines() if line.strip()]
        lines += ["== modules", " ".join(m for m in modules if _REPORT_MODULES.match(m)) or "none of the usual ones"]
        lines.append("== drives")
        lines += [f"{d.name} {d.bus or '-'} {d.model or '-'}" for d in self.drives()]
        lines += ["== bmc", "/dev/ipmi0" if (self._dev / "ipmi0").exists() else "no /dev/ipmi0"]
        return "\n".join(lines)

    def drives(self) -> list[Drive]:
        """List the whole drives that have a name in /dev/disk/by-id."""
        by_id = self._dev / "disk" / "by-id"
        found: dict[str, Drive] = {}
        try:
            entries = sorted(by_id.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        for entry in entries:
            if _PART.search(entry.name):
                continue
            try:
                kernel_name = entry.resolve().name
            except OSError:
                continue
            drive = found.get(kernel_name)
            if drive is None:
                drive = found[kernel_name] = self._drive(kernel_name)
            drive.ids.append(f"/dev/disk/by-id/{entry.name}")
        for drive in found.values():
            drive.ids.sort(key=lambda p: (Path(p).name.startswith(_LOW_PRIORITY), p))
        return sorted(found.values(), key=lambda d: _natural_key(d.name))

    def _drive(self, kernel_name: str) -> Drive:
        """Read what sysfs says about a drive."""
        block = self._sys / "class" / "block" / kernel_name
        size = _read_int(block / "size")
        rotational = _read_int(block / "queue" / "rotational")
        bus, controller, link = self._connection(block, kernel_name)
        return Drive(
            name=kernel_name,
            model=_read(block / "device" / "model") or "",
            size_bytes=(size or 0) * 512,
            rotational=None if rotational is None else rotational == 1,
            bus=bus,
            controller=controller,
            link=link,
            io=self._io(block),
        )

    @staticmethod
    def _io(block: Path) -> DriveIo | None:
        """Read the I/O counters of a drive, or None when sysfs has none.

        The fields of the stat file: sectors read is the third, sectors written
        the seventh, requests in flight the ninth and the time with I/O under
        way (ms) the tenth.
        """
        text = _read(block / "stat")
        try:
            fields = [int(value) for value in (text or "").split()]
        except ValueError:
            return None
        if len(fields) < 10:
            return None
        return DriveIo(busy_ms=fields[9], read_sectors=fields[2], write_sectors=fields[6], in_flight=fields[8])

    @staticmethod
    def _connection(block: Path, kernel_name: str) -> tuple[str, str, str]:
        """Return the bus, the driver of the controller and the PCIe link of a drive, as far as sysfs tells.

        /sys/class/block/sda points at its place in the device tree, for example
        /sys/devices/pci0000:00/0000:00:17.0/ata3/host2/... (a SATA port of the
        board) or .../0000:01:00.0/host0/port-0:0/end_device-0:0/... (an HBA).
        """
        try:
            parts = block.resolve(strict=True).parts
        except OSError:
            return "", "", ""
        if any(part.startswith("usb") for part in parts):
            bus = "usb"
        elif kernel_name.startswith("nvme"):
            bus = "nvme"
        elif any(_ATA_PORT.match(part) for part in parts):
            bus = "sata"
        elif any(part.startswith(("end_device-", "port-", "expander-")) for part in parts):
            bus = "sas"
        else:
            bus = ""
        controller = ""
        pci = [index for index, part in enumerate(parts) if _PCI_ADDRESS.match(part)]
        if pci:
            with contextlib.suppress(OSError):
                controller = (Path(*parts[: pci[-1] + 1]) / "driver").resolve(strict=True).name
        link = ""
        if bus == "nvme":
            speed = _read(block / "device" / "device" / "current_link_speed")
            width = _read(block / "device" / "device" / "current_link_width")
            if speed:
                link = f"{speed} x{width}" if width else speed
        return bus, controller, link

    def hwmons(self) -> list[HwmonDevice]:
        """List the hwmon devices, with their PWM outputs and temperatures."""
        base = self._sys / "class" / "hwmon"
        try:
            names = sorted((p.name for p in base.iterdir() if _HWMON.match(p.name)), key=_natural_key)
        except OSError:
            names = []
        devices = [self._hwmon(base / name) for name in names]
        bmc = self._bmc() if self._bmc is not None else None
        return devices + [bmc] if bmc is not None else devices

    def _hwmon(self, directory: Path) -> HwmonDevice:
        """Read one hwmon device."""
        real = f"/sys/class/hwmon/{directory.name}"
        device = HwmonDevice(hwmon=directory.name, name=_read(directory / "name") or "")
        try:
            files = sorted((p.name for p in directory.iterdir()), key=_natural_key)
        except OSError:
            return device
        for name in files:
            if match := _PWM.match(name):
                number = match.group(1)
                device.pwms.append(
                    PwmOutput(
                        path=f"{real}/{name}",
                        value=_read_int(directory / name),
                        enable=_read_int(directory / f"pwm{number}_enable"),
                        rpm=_read_int(directory / f"fan{number}_input"),
                    )
                )
            elif match := _TEMP.match(name):
                number = match.group(1)
                millidegrees = _read_int(directory / name)
                device.temps.append(
                    TempInput(
                        path=f"{real}/{name}",
                        label=_read(directory / f"temp{number}_label") or f"temp{number}",
                        celsius=None if millidegrees is None else millidegrees / 1000,
                    )
                )
        return device

    def read_only(self) -> bool:
        """Whether /sys is mounted read only, as in a container that is not privileged.

        Then no PWM file can be written, whatever the permissions say.
        """
        try:
            return bool(os.statvfs(self._sys).f_flag & os.ST_RDONLY)
        except OSError:
            return True

    def chip_of(self, path: str) -> str | None:
        """Return the stable /sys/devices path of the chip that owns a hwmon file, or None.

        /sys/class/hwmon/hwmonN numbers follow the order in which drivers
        appear and can change at a reboot; the device link of the hwmon folder
        (/sys/devices/platform/nct6775.656 for example) does not.
        """
        if not path.startswith("/sys/"):
            return None  # the fans of a BMC have no chip in /sys
        folder = self._sys / os.path.dirname(path.removeprefix("/sys/"))
        try:
            real = (folder / "device").resolve(strict=True)
            return "/sys/" + str(real.relative_to(self._sys.resolve()))
        except (OSError, ValueError):
            return None

    def locate(self, path: str, chip: str) -> str | None:
        """Return where the file named like path is now on that chip, or None if the chip is gone."""
        if self.chip_of(path) == chip and (self._sys / path.removeprefix("/sys/")).exists():
            return path
        name = os.path.basename(path)
        base = self._sys / "class" / "hwmon"
        try:
            folders = sorted((p.name for p in base.iterdir() if _HWMON.match(p.name)), key=_natural_key)
        except OSError:
            return None
        for folder in folders:
            candidate = f"/sys/class/hwmon/{folder}/{name}"
            if self.chip_of(candidate) == chip and (base / folder / name).exists():
                return candidate
        return None

    def kernel_names(self) -> dict[str, str]:
        """Map the stable /dev paths of whole drives (by-id, by-path) and /dev/sdX to their kernel name (sdX).

        The temperature log names drives by kernel name; the settings by
        these paths. Only reads the links: no drive is touched.
        """
        names: dict[str, str] = {}
        for folder in ("by-id", "by-path"):
            base = self._dev / "disk" / folder
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                if _PART.search(entry.name):
                    continue
                try:
                    kernel = entry.resolve().name
                except OSError:
                    continue
                names[f"/dev/disk/{folder}/{entry.name}"] = kernel
                names[f"/dev/{kernel}"] = kernel
        return names

    def dev_exists(self, path: str) -> bool:
        """Whether a /dev path (a by-id link or a device) leads to something now."""
        return (self._dev / path.removeprefix("/dev/")).exists()

    def is_block_device(self, path: str) -> bool | None:
        """Whether a /dev path leads to a block device: None when it leads nowhere (an unplugged drive)."""
        try:
            mode = (self._dev / path.removeprefix("/dev/")).stat().st_mode
        except FileNotFoundError:
            return None
        except OSError:
            return False
        return bool(_IS_DRIVE_NODE(mode))

    def cpu(self, hwmons: list[HwmonDevice] | None = None) -> Cpu:
        """Read the processors: /proc/stat, /proc/loadavg and the package temperature of their hwmon chip."""
        cpu = Cpu()
        stat_text = _read(self._proc / "stat") or ""
        for line in stat_text.splitlines():
            name, _, rest = line.partition(" ")
            if name == "cpu":
                try:
                    ticks = [int(value) for value in rest.split()]
                except ValueError:
                    continue
                # user nice system idle iowait irq softirq steal; guest time is already in user.
                ticks = ticks[:8]
                if len(ticks) >= 4:
                    idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
                    cpu.total = sum(ticks)
                    cpu.busy = cpu.total - idle
            elif name.startswith("cpu") and name[3:].isdigit():
                cpu.count += 1
        load_text = _read(self._proc / "loadavg") or ""
        with contextlib.suppress(ValueError):
            cpu.load = [float(value) for value in load_text.split()[:3]]
        cpu.temp, cpu.temp_path = self._cpu_temp(self.hwmons() if hwmons is None else hwmons)
        return cpu

    def cpu_temp(self) -> float | None:
        """The hottest package temperature of the processors, reading only their hwmon chips.

        Light enough to be read every second (the CPU curve of a BMC, bmc.py).
        """
        base = self._sys / "class" / "hwmon"
        try:
            folders = [p for p in base.iterdir() if _HWMON.match(p.name)]
        except OSError:
            return None
        chips = [self._hwmon(folder) for folder in folders if (_read(folder / "name") or "") in _CPU_CHIPS]
        return self._cpu_temp(chips)[0]

    @staticmethod
    def _cpu_temp(hwmons: list[HwmonDevice]) -> tuple[float | None, str]:
        """The hottest package temperature of the processor chips, and its path."""
        found: list[TempInput] = []
        for device in hwmons:
            preferred = _CPU_CHIPS.get(device.name)
            if preferred is None:
                continue
            readable = [temp for temp in device.temps if temp.celsius is not None]
            packages = [temp for temp in readable if temp.label in preferred]
            if device.name in ("k10temp", "zenpower") and packages:
                # Tdie, when there, has no offset; Tctl may have one.
                packages = [min(packages, key=lambda temp: preferred.index(temp.label))]
            found += packages or readable[:1]
        if not found:
            return None, ""
        hottest = max(found, key=lambda temp: temp.celsius or 0.0)
        return hottest.celsius, hottest.path

    def pwm_paths(self) -> set[str]:
        """Return the paths of every PWM output, to check what a client asks to test."""
        return {pwm.path for device in self.hwmons() for pwm in device.pwms}

    def snapshot(self) -> dict[str, Any]:
        """Return drives, hwmon devices and processors as JSON-ready data.

        clock (seconds, monotonic) dates the counters, so the page can turn two
        snapshots into rates whatever the delay of the network.
        """
        hwmons = self.hwmons()
        return {
            "clock": time.monotonic(),
            "drives": [asdict(d) for d in self.drives()],
            "hwmons": [asdict(h) for h in hwmons],
            "cpu": asdict(self.cpu(hwmons)),
        }
