"""What the host offers: drives, PWM outputs, fan speeds and temperature inputs.

Everything here reads sysfs and /dev only. Nothing sends a command to a
drive, so a drive that sleeps is never woken up by the web UI: the drive
temperatures come from the log that hddfancontrol writes (see templog.py).

The paths returned are always the real ones (/dev/..., /sys/...), the ones
hddfancontrol needs, even when the tests read a fake tree somewhere else.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_PART = re.compile(r"-part\d+$")
# Several names point at the same drive. These say less than the model and
# serial names, and only show when a drive has no other one.
_LOW_PRIORITY = ("wwn-", "nvme-eui.", "nvme-nvme.", "eui.")
_PWM = re.compile(r"^pwm(\d+)$")
_TEMP = re.compile(r"^temp(\d+)_input$")
_HWMON = re.compile(r"^hwmon\d+$")


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

    def __init__(self, dev_root: Path = Path("/dev"), sys_root: Path = Path("/sys")) -> None:
        """Read from these trees: the real ones, or a copy in the tests."""
        self._dev = dev_root
        self._sys = sys_root

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
        return Drive(
            name=kernel_name,
            model=_read(block / "device" / "model") or "",
            size_bytes=(size or 0) * 512,
            rotational=None if rotational is None else rotational == 1,
        )

    def hwmons(self) -> list[HwmonDevice]:
        """List the hwmon devices, with their PWM outputs and temperatures."""
        base = self._sys / "class" / "hwmon"
        try:
            names = sorted((p.name for p in base.iterdir() if _HWMON.match(p.name)), key=_natural_key)
        except OSError:
            return []
        return [self._hwmon(base / name) for name in names]

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

    def pwm_paths(self) -> set[str]:
        """Return the paths of every PWM output, to check what a client asks to test."""
        return {pwm.path for device in self.hwmons() for pwm in device.pwms}

    def snapshot(self) -> dict[str, Any]:
        """Return drives and hwmon devices as JSON-ready data."""
        return {
            "drives": [asdict(d) for d in self.drives()],
            "hwmons": [asdict(h) for h in self.hwmons()],
        }
