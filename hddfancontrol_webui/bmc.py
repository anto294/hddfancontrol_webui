"""The baseboard management controller of a server (Dell iDRAC, HPE iLO), through ipmitool.

EXPERIMENTAL. Servers such as the Dell PowerEdge R730 or the HPE ProLiant
ML350 drive their fans from the BMC: Linux sees no PWM output for them. The
web UI talks to the BMC of the host it runs on through /dev/ipmi0 (kernel
modules ipmi_si and ipmi_devintf), with ipmitool, never over the network.

- Dell (iDRAC 7/8, PowerEdge 12th and 13th generation): the fans can be
  driven. Dell's undocumented OEM commands take fan control from the iDRAC
  (raw 0x30 0x30 0x01 0x00), set every fan to a percentage
  (raw 0x30 0x30 0x02 0xff <percent>) and give control back
  (raw 0x30 0x30 0x01 0x01).
- HPE (iLO 4/5) and any other BMC: read only. HPE offers no way to set a
  fan speed; the temperatures and fan speeds of the BMC are shown.

The server model (MODELS, found in the FRU data or chosen in the IPMI tab)
gives the minimum speed to start from and the sensors of the processors.
Its configuration (/config/bmc.json, apart from the settings: changing it
never restarts fan control) says who drives the fans of a Dell: the iDRAC
(the default) or the web UI. The web UI runs them at the highest of the
minimum speed, the CPU curve (a profile, or the user's own points) and
what the drives of their fan group ask (BMC_PWM, written by the fan
controller). The CPU curve follows the processor temperature of the host
(coretemp, k10temp), else the processor sensors of the BMC; it goes up at
once and down by CPU_STEP_DOWN percent a second.

Safety. While the web UI drives them, the iDRAC no longer cools anything by
itself, and the fans keep the last speed set until someone changes it. So
control goes back to the iDRAC (its own automatic mode):
- at start (a run that was killed may have left them in manual mode) and
  at close, whatever the settings;
- while the fans belong to a fan group and the fan controller has given
  them back (stop, apply, fan control not running);
- when the sensors of the BMC cannot be read for STALE_S, or no processor
  temperature is known;
- while a temperature of the BMC is within HANDBACK_MARGIN of its upper
  non-critical threshold (or above FALLBACK_LIMIT when the BMC gives
  none), or the BMC reports it as not ok; it is taken again
  HANDBACK_HYSTERESIS degrees lower. Full speed is always applied: it
  cools everything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .const import BMC_PWM, BMC_TEMP_PREFIX
from .hardware import HwmonDevice, PwmOutput, TempInput
from .storage import write_atomic

IPMI_DEVICE = Path("/dev/ipmi0")
IPMITOOL_BIN = "ipmitool"
BMC_FILE = "bmc.json"
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
# How often the speed is worked out (processor temperature, what the fan
# controller asks) and sent when it changed.
APPLY_S = 1.0
# How fast the CPU curve comes down, in percent per APPLY_S: a load that
# comes and goes does not make the fans come and go.
CPU_STEP_DOWN = 1.0
HANDBACK_MARGIN = 5.0
HANDBACK_HYSTERESIS = 3.0
FALLBACK_LIMIT = 80.0
PWM_MAX = 255

DELL_MANUAL = ("raw", "0x30", "0x30", "0x01", "0x00")
DELL_AUTO = ("raw", "0x30", "0x30", "0x01", "0x01")
DELL_SPEED = ("raw", "0x30", "0x30", "0x02", "0xff")

CONTROLS = ("bmc", "webui")
PROFILES: dict[str, tuple[tuple[float, float], ...]] = {
    # Processor temperature (°C) -> fan speed (%), straight lines in between;
    # below the first point the first speed, never under the minimum speed.
    "silent": ((50, 0), (65, 20), (75, 45), (85, 100)),
    "normal": ((45, 0), (60, 25), (70, 50), (80, 100)),
    "turbo": ((40, 20), (55, 45), (65, 75), (75, 100)),
}
CUSTOM = "custom"
CURVE_POINTS = (2, 8)
CURVE_TEMPS = (20.0, 105.0)
MIN_PERCENT = (5, 100)

Log = Callable[[str], None]


# ---------------------------
#   Model
# ---------------------------
@dataclass(frozen=True)
class Model:
    """A server: its BMC vendor, whether the web UI can drive its fans, where to start.

    min_percent is a cautious starting point for the minimum speed (more
    drives, more air); cpu_sensors are the names of the processor
    temperatures of its BMC, used when the host shows none.
    """

    id: str
    vendor: str | None
    name: str
    control: bool
    min_percent: int = 20
    cpu_sensors: tuple[str, ...] = ()


_DELL_CPU = ("Temp", "CPU1 Temp", "CPU2 Temp")
_HPE_CPU = ("02-CPU 1", "03-CPU 2")
MODELS: dict[str, Model] = {
    model.id: model
    for model in (
        Model("dell-r620", DELL, "PowerEdge R620", True, 15, _DELL_CPU),
        Model("dell-r720", DELL, "PowerEdge R720", True, 15, _DELL_CPU),
        Model("dell-r720xd", DELL, "PowerEdge R720xd", True, 20, _DELL_CPU),
        Model("dell-t620", DELL, "PowerEdge T620", True, 10, _DELL_CPU),
        Model("dell-r630", DELL, "PowerEdge R630", True, 15, _DELL_CPU),
        Model("dell-r730", DELL, "PowerEdge R730", True, 15, _DELL_CPU),
        Model("dell-r730xd", DELL, "PowerEdge R730xd", True, 20, _DELL_CPU),
        Model("dell-t630", DELL, "PowerEdge T630", True, 10, _DELL_CPU),
        Model("dell-other", DELL, "PowerEdge", True, 20, _DELL_CPU),
        Model("hpe-ml350-gen9", HPE, "ProLiant ML350 Gen9", False, cpu_sensors=_HPE_CPU),
        Model("hpe-ml350-gen10", HPE, "ProLiant ML350 Gen10", False, cpu_sensors=_HPE_CPU),
        Model("hpe-dl380-gen9", HPE, "ProLiant DL380 Gen9", False, cpu_sensors=_HPE_CPU),
        Model("hpe-dl380-gen10", HPE, "ProLiant DL380 Gen10", False, cpu_sensors=_HPE_CPU),
        Model("hpe-other", HPE, "ProLiant", False, cpu_sensors=_HPE_CPU),
        Model("other", None, "", False),
    )
}


# ---------------------------
#   BmcError
# ---------------------------
class BmcError(Exception):
    """ipmitool failed, timed out or is missing."""


# ---------------------------
#   BmcConfigError
# ---------------------------
class BmcConfigError(ValueError):
    """A configuration that cannot be used: problems like those of the settings ({field, code, params})."""

    def __init__(self, problems: list[dict[str, Any]]) -> None:
        """Keep the problems."""
        self.problems = problems
        super().__init__(", ".join(p["code"] for p in problems))


# ---------------------------
#   BmcConfig
# ---------------------------
@dataclass(frozen=True)
class BmcConfig:
    """Who drives the fans, and how: the model ("" found by itself), the CPU profile and its curve."""

    model: str = ""
    control: str = "bmc"
    profile: str = "normal"
    curve: tuple[tuple[float, float], ...] = PROFILES["normal"]
    min_percent: int = 20

    def points(self) -> tuple[tuple[float, float], ...]:
        """The CPU curve in use: the profile's, or the user's own."""
        return self.curve if self.profile == CUSTOM else PROFILES[self.profile]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready data, the shape check_config reads."""
        data = asdict(self)
        data["curve"] = [list(point) for point in self.curve]
        return data


# ---------------------------
#   _Problems
# ---------------------------
class _Problems:
    """Collect the problems of a configuration."""

    def __init__(self) -> None:
        """No problem yet."""
        self.items: list[dict[str, Any]] = []

    def add(self, field_name: str, code: str, **params: Any) -> None:
        """Record a problem."""
        self.items.append({"field": field_name, "code": code, "params": params})


# ---------------------------
#   _number_in
# ---------------------------
def _number_in(value: Any, low: float, high: float) -> float | None:
    """A number between low and high, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if low <= number <= high else None


# ---------------------------
#   check_config
# ---------------------------
def check_config(data: Any) -> BmcConfig:
    """Check a configuration document; BmcConfigError with every problem found."""
    if not isinstance(data, Mapping):
        raise BmcConfigError([{"field": "", "code": "invalid_document", "params": {}}])
    check = _Problems()
    defaults = BmcConfig()
    model = data.get("model", defaults.model)
    if not isinstance(model, str) or (model != "" and model not in MODELS):
        check.add("model", "invalid_choice", choices=", ".join(MODELS))
    control = data.get("control", defaults.control)
    if control not in CONTROLS:
        check.add("control", "invalid_choice", choices=", ".join(CONTROLS))
    profile = data.get("profile", defaults.profile)
    if profile not in (*PROFILES, CUSTOM):
        check.add("profile", "invalid_choice", choices=", ".join((*PROFILES, CUSTOM)))
    min_percent = data.get("min_percent", defaults.min_percent)
    if (
        isinstance(min_percent, bool)
        or not isinstance(min_percent, int)
        or not MIN_PERCENT[0] <= min_percent <= MIN_PERCENT[1]
    ):
        check.add("min_percent", "out_of_range", min=MIN_PERCENT[0], max=MIN_PERCENT[1])
    curve = _check_curve(check, data.get("curve", [list(p) for p in defaults.curve]))
    if check.items:
        raise BmcConfigError(check.items)
    return BmcConfig(str(model), str(control), str(profile), curve, int(min_percent))


# ---------------------------
#   _check_curve
# ---------------------------
def _check_curve(check: _Problems, value: Any) -> tuple[tuple[float, float], ...]:
    """Read the user's CPU curve: CURVE_POINTS points [°C, %], warmer and not slower each time."""
    if not isinstance(value, (list, tuple)) or not CURVE_POINTS[0] <= len(value) <= CURVE_POINTS[1]:
        check.add("curve", "curve_points", min=CURVE_POINTS[0], max=CURVE_POINTS[1])
        return ()
    points: list[tuple[float, float]] = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            check.add(f"curve.{index}", "invalid_document")
            return ()
        temp = _number_in(point[0], *CURVE_TEMPS)
        percent = _number_in(point[1], 0, 100)
        if temp is None:
            check.add(f"curve.{index}.0", "out_of_range", min=CURVE_TEMPS[0], max=CURVE_TEMPS[1])
            return ()
        if percent is None:
            check.add(f"curve.{index}.1", "out_of_range", min=0, max=100)
            return ()
        if points and (temp <= points[-1][0] or percent < points[-1][1]):
            check.add(f"curve.{index}", "curve_order")
            return ()
        points.append((temp, percent))
    return tuple(points)


# ---------------------------
#   curve_percent
# ---------------------------
def curve_percent(points: tuple[tuple[float, float], ...], temp: float) -> float:
    """The fan speed (%) of a curve at a temperature: straight lines between the points, flat outside."""
    if temp <= points[0][0]:
        return points[0][1]
    for (t1, p1), (t2, p2) in zip(points, points[1:], strict=False):
        if temp <= t2:
            return p1 + (p2 - p1) * (temp - t1) / (t2 - t1)
    return points[-1][1]


# ---------------------------
#   detect_model
# ---------------------------
def detect_model(vendor: str | None, product: str) -> str:
    """The model whose name is in the product name of the FRU data, else the vendor's other model."""
    text = product.lower()
    found = [m for m in MODELS.values() if m.vendor == vendor and m.name and m.name.lower() in text]
    if found:
        return max(found, key=lambda m: len(m.name)).id
    return {DELL: "dell-other", HPE: "hpe-other"}.get(vendor or "", "other")


# ---------------------------
#   catalogue
# ---------------------------
def catalogue() -> dict[str, Any]:
    """The models and the CPU profiles, for the IPMI tab."""
    return {
        "models": [
            {"id": m.id, "vendor": m.vendor, "name": m.name, "control": m.control, "min_percent": m.min_percent}
            for m in MODELS.values()
        ],
        "profiles": {name: [list(point) for point in points] for name, points in PROFILES.items()},
        "limits": {"points": list(CURVE_POINTS), "temps": list(CURVE_TEMPS), "min_percent": list(MIN_PERCENT)},
    }


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
#   parse_fields
# ---------------------------
def parse_fields(text: str) -> dict[str, str]:
    """The 'key : value' lines of ipmitool (mc info, fru print), keys in lower case; the first one wins."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key.strip().lower(), value.strip())
    return fields


# ---------------------------
#   parse_vendor
# ---------------------------
def parse_vendor(text: str) -> tuple[str | None, str]:
    """The vendor (dell, hpe or None) and the product name in the output of 'ipmitool mc info'."""
    fields = parse_fields(text)
    vendor: str | None = None
    with contextlib.suppress(ValueError, IndexError):
        vendor = VENDORS.get(int(fields.get("manufacturer id", "").split()[0]))
    return vendor, fields.get("product name", "")


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
    cpu_temp: float | None = None
    cpu_percent: float | None = None


class Bmc:
    """The BMC of the host: its sensors, and its fans on a Dell server."""

    def __init__(
        self,
        program: str = IPMITOOL_BIN,
        device: Path = IPMI_DEVICE,
        log: Log | None = None,
        config_dir: Path | None = None,
        cpu_temp: Callable[[], float | None] | None = None,
        in_group: Callable[[], bool] | None = None,
    ) -> None:
        """Use program (ipmitool) on device (/dev/ipmi0); log what changes with log.

        config_dir holds bmc.json (None: the configuration is not kept);
        cpu_temp reads the processor temperature of the host (blocking);
        in_group says whether BMC_PWM belongs to a fan group of the settings.
        """
        self._program = program
        self._device = device
        self._log = log or (lambda _text: None)
        self._path = config_dir / BMC_FILE if config_dir is not None else None
        self.host_cpu_temp = cpu_temp or (lambda: None)
        self.in_group = in_group or (lambda: False)
        self.vendor: str | None = None
        self.product = ""
        self.firmware = ""
        self.detected = "other"
        self.present = False
        self.state = _State()
        self.config = self._load()
        # The fan controller writes from the event loop or from a worker
        # thread; the loop of this class sends what it asked.
        self._lock = threading.Lock()
        self._desired: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._clock: Callable[[], float] = time.monotonic

    def _load(self) -> BmcConfig:
        """Read bmc.json, or start from the defaults (the iDRAC keeps its fans)."""
        if self._path is None:
            return BmcConfig()
        try:
            return check_config(json.loads(self._path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return BmcConfig()
        except Exception as err:  # unreadable or refused: never a failed start, the iDRAC keeps its fans
            self._log(f"ERROR: cannot read {self._path}, the BMC keeps its fans: {err!r}")
            return BmcConfig()

    @property
    def name(self) -> str:
        """iDRAC, iLO or BMC."""
        return VENDOR_NAMES.get(self.vendor or "", "BMC")

    @property
    def model(self) -> Model:
        """The model chosen, else the one found."""
        return MODELS[self.config.model or self.detected]

    @property
    def can_drive(self) -> bool:
        """Whether the web UI can set the speed of the fans of this BMC."""
        return self.present and self.vendor == DELL and self.model.vendor == DELL and self.model.control

    @property
    def drives_fans(self) -> bool:
        """Whether the web UI is asked to drive the fans (not whether it does now)."""
        return self.can_drive and self.config.control == "webui"

    def set_config(self, data: Any) -> BmcConfig:
        """Check and keep a new configuration; it is used within APPLY_S. Blocking (writes bmc.json)."""
        config = check_config(data)
        model = MODELS[config.model] if config.model else None
        if self.present and model is not None and model.vendor is not None and model.vendor != self.vendor:
            raise BmcConfigError([{"field": "model", "code": "bmc_model_vendor", "params": {"name": self.name}}])
        if self._path is not None:
            write_atomic(self._path, json.dumps(config.to_dict(), indent=2) + "\n")
        with self._lock:
            self.config = config
        return config

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
        except asyncio.CancelledError:
            # The request may already be with the BMC: let it end, so that a
            # command sent after this one (DELL_AUTO at close) lands last.
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(asyncio.shield(process.wait()), TIMEOUT_S)
            raise
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
        """Find the BMC and the server, give a Dell its fans back and start reading the sensors.

        Nothing happens without /dev/ipmi0 or ipmitool: most hosts have no BMC.
        """
        if not self._device.exists() or shutil.which(self._program) is None:
            return
        try:
            info = await self._run("mc", "info")
        except BmcError as err:
            self._log(f"WARNING: {self._device} is there, but the BMC does not answer: {err}")
            return
        self.vendor, self.product = parse_vendor(info)
        self.firmware = parse_fields(info).get("firmware revision", "")
        # The server's own name (PowerEdge R730xd) is in the FRU data; the BMC's (iDRAC8) is the fallback.
        with contextlib.suppress(BmcError):
            fru = parse_fields(await self._run("fru", "print", "0"))
            self.product = fru.get("product name") or fru.get("board product") or self.product
        self.detected = detect_model(self.vendor, self.product)
        self.present = True
        what = "its fans can be driven (experimental)" if self.can_drive else "read only"
        found = " ".join(part for part in (self.name, self.firmware, "on", self.product) if part)
        self._log(f"BMC found: {found}, {what}")
        if self.vendor == DELL:
            await self._auto()
        await self._poll()
        self._task = asyncio.create_task(self._loop(), name="bmc")

    async def close(self) -> None:
        """Stop, and give a Dell its fans back if the web UI had them."""
        if self._task is not None:
            await cancel_and_wait(self._task)
            self._task = None
        if self.vendor == DELL and self.state.manual:
            await self._auto()

    def write(self, value: int) -> None:
        """What the drives of the fan group ask: a PWM value (0-255) on every fan."""
        with self._lock:
            self._desired = max(0, min(PWM_MAX, int(value)))

    def release(self) -> None:
        """The fan controller gives the fans back: the iDRAC takes them while they belong to a fan group."""
        with self._lock:
            self._desired = None

    async def _loop(self) -> None:
        """Work out and send the speed every APPLY_S, read the sensors every POLL_S."""
        next_poll = self._clock() + POLL_S
        while True:
            await asyncio.sleep(APPLY_S)
            try:
                if self._clock() >= next_poll:
                    await self._poll()
                    next_poll = self._clock() + POLL_S
                if self.vendor == DELL:
                    await self._apply()
            except Exception as err:  # any bug: the iDRAC gets its fans back, and the loop goes on
                self._log(f"ERROR: unexpected error, the fans go back to the {self.name}: {err!r}")
                if self.vendor == DELL:
                    await self._auto()

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

    def _cpu_temp(self) -> float | None:
        """The processor temperature: the host's, else the hottest processor sensor of the BMC. Blocking."""
        try:
            temp = self.host_cpu_temp()
        except Exception:  # an unreadable /sys file: the BMC's sensors are there for that
            temp = None
        if temp is not None:
            return temp
        names = self.model.cpu_sensors
        found = [
            r.value
            for r in self.state.readings
            if r.kind == "temp" and r.value is not None and (r.name in names or "cpu" in r.name.lower())
        ]
        return max(found) if found else None

    def _cpu_level(self, temp: float | None) -> float | None:
        """The speed the CPU curve asks, up at once and down by CPU_STEP_DOWN; None without a temperature."""
        self.state.cpu_temp = temp
        if temp is None:
            self.state.cpu_percent = None
            return None
        wanted = curve_percent(self.config.points(), temp)
        before = self.state.cpu_percent
        level = wanted if before is None or wanted >= before else max(wanted, before - CPU_STEP_DOWN)
        self.state.cpu_percent = level
        return level

    def _handback_reason(self) -> str | None:
        """Why control must stay with the BMC now, or None."""
        if self.state.read_at is None or self._clock() - self.state.read_at > STALE_S:
            return "stale"
        hot = hot_sensor(self.state.readings, self.state.handback is not None)
        return None if hot is None else f"{hot.name} {hot.value:g} °C"

    def _target(self, desired: int | None, cpu: float | None) -> tuple[int | None, str | None]:
        """The speed to send (None: the iDRAC drives) and why the iDRAC has them, if it should."""
        if not self.drives_fans:
            return None, None
        if desired is None and self.in_group():
            # The fan controller is not running: the drives would not be followed.
            return None, None
        drives = percent_of(desired) if desired is not None else 0
        if drives >= 100:
            # Full speed is never handed back: it cools everything.
            return 100, None
        if cpu is None:
            return None, "no_cpu_temp"
        percent = round(max(self.config.min_percent, cpu, drives))
        return percent, (self._handback_reason() if percent < 100 else None)

    async def _apply(self) -> None:
        """Bring the fans to the speed asked, or back to the iDRAC when the rules say so."""
        with self._lock:
            desired = self._desired
        if self.drives_fans:
            cpu = self._cpu_level(await asyncio.to_thread(self._cpu_temp))
        else:
            # The iDRAC drives: a curve taken up again starts from the temperature of then.
            cpu = self.state.cpu_temp = self.state.cpu_percent = None
        percent, reason = self._target(desired, cpu)
        if reason is not None and self.state.handback is None:
            self._log(f"The fans go back to the {self.name}: {reason}")
        elif reason is None and self.state.handback is not None and percent is not None:
            self._log(f"The web UI drives the fans again ({self.state.handback} is over)")
        self.state.handback = reason
        try:
            if percent is None or reason is not None:
                if self.state.manual:
                    await self._auto()
                return
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
        with self._lock:
            desired = self._desired
        return {
            "name": self.name,
            "product": self.product,
            "firmware": self.firmware,
            "model": self.model.id,
            "can_drive": self.can_drive,
            "drives_fans": self.drives_fans,
            "in_group": self.in_group(),
            "manual": self.state.manual,
            "percent": self.state.percent,
            "cpu_temp": self.state.cpu_temp,
            "cpu_percent": None if self.state.cpu_percent is None else round(self.state.cpu_percent),
            "drives_percent": None if desired is None else percent_of(desired),
            "handback": self.state.handback,
            "error": self.state.error,
            "readings": [
                {"name": r.name, "kind": r.kind, "value": r.value, "status": r.status, "limit": r.limit}
                for r in self.state.readings
            ],
        }

    def describe(self) -> dict[str, Any] | None:
        """The IPMI tab's data: the configuration, the model found, the catalogue; None without a BMC."""
        if not self.present:
            return None
        return {"config": self.config.to_dict(), "detected": self.detected, "vendor": self.vendor, **catalogue()}
