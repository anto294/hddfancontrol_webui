"""The baseboard management controller of a server (Dell iDRAC, HPE iLO), through ipmitool.

EXPERIMENTAL. Servers such as the Dell PowerEdge R730 or the HPE ProLiant
ML350 drive their fans from the BMC: Linux sees no PWM output for them. The
web UI talks to the BMC of the host it runs on through /dev/ipmi0 (kernel
modules ipmi_si and ipmi_devintf), with ipmitool, never over the network.

- Dell (iDRAC 7/8, PowerEdge 12th and 13th generation): the fans can be
  driven. Dell's undocumented OEM commands take fan control from the iDRAC
  (raw 0x30 0x30 0x01 0x00), set every fan to a percentage
  (raw 0x30 0x30 0x02 0xff <percent>) and give control back
  (raw 0x30 0x30 0x01 0x01). They show as one PWM output, BMC_PWM.
- HPE (iLO 4/5) and any other BMC: read only. HPE offers no way to set a
  fan speed; the temperatures and fan speeds of the BMC are shown.

Safety. While the web UI drives them, the iDRAC no longer cools the
processors by itself, and the fans keep the last speed set until someone
changes it. So control goes back to the iDRAC (its own automatic mode):
- whenever the web UI gives the fans back (stop, apply, exit), at start
  (a run that was killed may have left them in manual mode), and when
  the sensors cannot be read for STALE_S;
- while a temperature of the BMC is within HANDBACK_MARGIN of its upper
  non-critical threshold (or above FALLBACK_LIMIT when the BMC gives
  none), or the BMC reports it as not ok; it is taken again
  HANDBACK_HYSTERESIS degrees lower. Full speed (255) is always applied:
  it cools everything.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .const import BMC_PWM, BMC_TEMP_PREFIX
from .hardware import HwmonDevice, PwmOutput, TempInput

IPMI_DEVICE = Path("/dev/ipmi0")
IPMITOOL_BIN = "ipmitool"
DELL = "dell"
HPE = "hpe"
# IANA enterprise numbers in "Manufacturer ID" of "ipmitool mc info".
VENDORS = {674: DELL, 11: HPE, 47196: HPE}
VENDOR_NAMES = {DELL: "iDRAC", HPE: "iLO"}
TIMEOUT_S = 10.0
# How often the sensors are read, and after how long without a reading
# control goes back to the BMC.
POLL_S = 30.0
STALE_S = 3 * POLL_S
# How often a new speed asked by the fan controller is sent.
APPLY_S = 1.0
HANDBACK_MARGIN = 5.0
HANDBACK_HYSTERESIS = 3.0
FALLBACK_LIMIT = 80.0
PWM_MAX = 255

DELL_MANUAL = ("raw", "0x30", "0x30", "0x01", "0x00")
DELL_AUTO = ("raw", "0x30", "0x30", "0x01", "0x01")
DELL_SPEED = ("raw", "0x30", "0x30", "0x02", "0xff")

Log = Callable[[str], None]


# ---------------------------
#   BmcError
# ---------------------------
class BmcError(Exception):
    """ipmitool failed, timed out or is missing."""


# ---------------------------
#   Reading
# ---------------------------
@dataclass(frozen=True)
class Reading:
    """One sensor of the BMC: a temperature (°C), a fan (RPM) or a fan duty (percent)."""

    name: str
    kind: str
    value: float | None
    status: str
    limit: float | None = None


# ---------------------------
#   _number
# ---------------------------
def _number(text: str) -> float | None:
    """A number of an ipmitool column, or None for na and the like."""
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------
#   parse_sensors
# ---------------------------
def parse_sensors(text: str) -> list[Reading]:
    """Read the output of 'ipmitool sensor'.

    Each line is name | value | unit | status | lnr | lcr | lnc | unc | ucr | unr.
    The limit of a temperature is its upper non-critical threshold, else
    its upper critical one. Discrete sensors and others are skipped.
    """
    readings: list[Reading] = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or not cells[0]:
            continue
        name, value, unit, status = cells[0], _number(cells[1]), cells[2].lower(), cells[3].lower()
        if unit == "degrees c":
            limit = None
            if len(cells) >= 9:
                limit = _number(cells[7])
                if limit is None:
                    limit = _number(cells[8])
            readings.append(Reading(name, "temp", value, status, limit))
        elif unit == "rpm":
            readings.append(Reading(name, "fan", value, status))
        elif unit == "percent" and "fan" in name.lower():
            readings.append(Reading(name, "fan_pct", value, status))
    return readings


# ---------------------------
#   parse_vendor
# ---------------------------
def parse_vendor(text: str) -> tuple[str | None, str]:
    """The vendor (dell, hpe or None) and the product name in the output of 'ipmitool mc info'."""
    vendor: str | None = None
    product = ""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "manufacturer id":
            with contextlib.suppress(ValueError):
                vendor = VENDORS.get(int(value.split()[0]))
        elif key == "product name":
            product = value
    return vendor, product


# ---------------------------
#   percent_of
# ---------------------------
def percent_of(value: int) -> int:
    """The fan duty (0-100) of a PWM value (0-255)."""
    return round(max(0, min(PWM_MAX, value)) * 100 / PWM_MAX)


# ---------------------------
#   hot_sensor
# ---------------------------
def hot_sensor(readings: list[Reading], handing_back: bool) -> Reading | None:
    """The temperature that makes control go back to the BMC, or None.

    A sensor counts when the BMC says it is not ok, or when it is within
    HANDBACK_MARGIN of its limit (FALLBACK_LIMIT without one); while control
    is already given back, HANDBACK_HYSTERESIS degrees lower, so it does not
    come and go around the threshold.
    """
    margin = HANDBACK_MARGIN + (HANDBACK_HYSTERESIS if handing_back else 0.0)
    for reading in readings:
        if reading.kind != "temp" or reading.value is None:
            continue
        if reading.status not in ("ok", "na", "ns", ""):
            return reading
        if reading.value >= (reading.limit if reading.limit is not None else FALLBACK_LIMIT) - margin:
            return reading
    return None


# ---------------------------
#   Bmc
# ---------------------------
@dataclass
class _State:
    """What the BMC does now, for the status and the hardware snapshot."""

    manual: bool = False
    percent: int | None = None
    handback: str | None = None
    error: str | None = None
    readings: list[Reading] = field(default_factory=list)
    read_at: float | None = None


class Bmc:
    """The BMC of the host: its sensors, and its fans on a Dell server."""

    def __init__(self, program: str = IPMITOOL_BIN, device: Path = IPMI_DEVICE, log: Log | None = None) -> None:
        """Use program (ipmitool) on device (/dev/ipmi0); log what changes with log."""
        self._program = program
        self._device = device
        self._log = log or (lambda _text: None)
        self.vendor: str | None = None
        self.product = ""
        self.present = False
        self.state = _State()
        # The fan controller writes from the event loop or from a worker
        # thread; the loop of this class sends what it asked.
        self._lock = threading.Lock()
        self._desired: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._clock: Callable[[], float] = time.monotonic

    @property
    def name(self) -> str:
        """iDRAC, iLO or BMC."""
        return VENDOR_NAMES.get(self.vendor or "", "BMC")

    @property
    def can_drive(self) -> bool:
        """Whether the web UI can set the speed of the fans of this BMC."""
        return self.present and self.vendor == DELL

    async def _run(self, *args: str) -> str:
        """Run ipmitool with args and return its output; BmcError on failure."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._program, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as err:
            raise BmcError(f"cannot run {self._program}: {err}") from err
        try:
            out, err_out = await asyncio.wait_for(process.communicate(), TIMEOUT_S)
        except TimeoutError as err:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise BmcError(f"{self._program} {' '.join(args)}: no answer in {TIMEOUT_S:g} s") from err
        if process.returncode != 0:
            message = err_out.decode(errors="replace").strip() or f"exit status {process.returncode}"
            raise BmcError(f"{self._program} {' '.join(args)}: {message}")
        return out.decode(errors="replace")

    async def start(self) -> None:
        """Find the BMC, give a Dell its fans back and start reading the sensors.

        Nothing happens without /dev/ipmi0 or ipmitool: most hosts have no BMC.
        """
        if not self._device.exists() or shutil.which(self._program) is None:
            return
        try:
            self.vendor, self.product = parse_vendor(await self._run("mc", "info"))
        except BmcError as err:
            self._log(f"WARNING: {self._device} is there, but the BMC does not answer: {err}")
            return
        self.present = True
        what = "its fans can be driven (experimental)" if self.can_drive else "read only"
        self._log(f"BMC found: {self.name} {self.product}".rstrip() + f", {what}")
        if self.can_drive:
            await self._auto()
        await self._poll()
        self._task = asyncio.create_task(self._loop(), name="bmc")

    async def close(self) -> None:
        """Stop, and give a Dell its fans back if the web UI had them."""
        if self._task is not None:
            await cancel_and_wait(self._task)
            self._task = None
        if self.can_drive and self.state.manual:
            await self._auto()

    def write(self, value: int) -> None:
        """Ask for this PWM value (0-255) on every fan; sent within APPLY_S."""
        with self._lock:
            self._desired = max(0, min(PWM_MAX, int(value)))

    def release(self) -> None:
        """Give the fans back to the BMC; sent within APPLY_S, and at close."""
        with self._lock:
            self._desired = None

    async def _loop(self) -> None:
        """Send what the fan controller asks, read the sensors every POLL_S."""
        next_poll = self._clock() + POLL_S
        while True:
            await asyncio.sleep(APPLY_S)
            if self._clock() >= next_poll:
                await self._poll()
                next_poll = self._clock() + POLL_S
            if self.can_drive:
                await self._apply()

    async def _poll(self) -> None:
        """Read the sensors; a failure is logged once and kept in the state."""
        try:
            readings = parse_sensors(await self._run("sensor"))
        except BmcError as err:
            if self.state.error is None:
                self._log(f"ERROR: cannot read the sensors of the {self.name}: {err}")
            self.state.error = str(err)
            return
        if self.state.error is not None:
            self._log(f"The sensors of the {self.name} are read again")
        self.state.error = None
        self.state.readings = readings
        self.state.read_at = self._clock()

    def _handback_reason(self) -> str | None:
        """Why control must stay with the BMC now, or None."""
        if self.state.read_at is None or self._clock() - self.state.read_at > STALE_S:
            return "stale"
        hot = hot_sensor(self.state.readings, self.state.handback is not None)
        return None if hot is None else f"{hot.name} {hot.value:g} °C"

    async def _apply(self) -> None:
        """Bring the fans to what is asked, or back to the iDRAC when the rules say so."""
        with self._lock:
            desired = self._desired
        if desired is None:
            reason = None
            self.state.handback = None
        else:
            # Full speed is never handed back: it cools everything.
            reason = self._handback_reason() if desired < PWM_MAX else None
            if reason is not None and self.state.handback is None:
                self._log(f"The fans go back to the {self.name}: {reason}")
            elif reason is None and self.state.handback is not None:
                self._log(f"The web UI drives the fans again (the {self.name} temperatures are back in range)")
            self.state.handback = reason
        try:
            if desired is None or reason is not None:
                if self.state.manual:
                    await self._auto()
                return
            percent = percent_of(desired)
            if not self.state.manual:
                await self._run(*DELL_MANUAL)
                self.state.manual = True
            if percent != self.state.percent:
                await self._run(*DELL_SPEED, f"0x{percent:02x}")
                self.state.percent = percent
        except BmcError as err:
            self._log(f"ERROR: {err}")
            # Unknown state: the next round tries again from scratch.
            self.state.percent = None

    async def _auto(self) -> None:
        """Give control back to the iDRAC; a failure is logged, never raised."""
        try:
            await self._run(*DELL_AUTO)
        except BmcError as err:
            self._log(f"ERROR: cannot give the fans back to the {self.name}: {err}")
            return
        self.state.manual = False
        self.state.percent = None

    def hwmon(self) -> HwmonDevice | None:
        """The BMC as a hwmon device of the hardware snapshot, or None without one.

        A Dell shows its fans as one PWM output, BMC_PWM, with the average
        speed; every BMC shows its temperatures, as inputs that only show.
        """
        if not self.present:
            return None
        device = HwmonDevice(hwmon="ipmi", name=self.name)
        fans = [r.value for r in self.state.readings if r.kind == "fan" and r.value is not None]
        if self.can_drive:
            value = None if not self.state.manual or self.state.percent is None else round(self.state.percent * PWM_MAX / 100)
            device.pwms.append(
                PwmOutput(
                    path=BMC_PWM,
                    value=value,
                    enable=1 if self.state.manual else 2,
                    rpm=round(sum(fans) / len(fans)) if fans else None,
                )
            )
        for reading in self.state.readings:
            if reading.kind == "temp":
                device.temps.append(
                    TempInput(path=f"{BMC_TEMP_PREFIX}{reading.name}", label=reading.name, celsius=reading.value)
                )
        return device

    def status(self) -> dict[str, Any] | None:
        """What the status of the web UI shows about the BMC, or None without one."""
        if not self.present:
            return None
        return {
            "name": self.name,
            "product": self.product,
            "drives_fans": self.can_drive,
            "manual": self.state.manual,
            "percent": self.state.percent,
            "handback": self.state.handback,
            "error": self.state.error,
            "readings": [
                {"name": r.name, "kind": r.kind, "value": r.value, "status": r.status, "limit": r.limit}
                for r in self.state.readings
            ],
        }
