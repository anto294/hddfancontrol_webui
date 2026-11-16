"""Settings of hddfancontrol and hd-idle.

The settings come from the environment variables of the container, with the
names of fightforlife/docker_hddfancontrol, until the web UI saves them to
``settings.json`` in the config directory. From then on the file wins, and
deleting it (Reset in the web UI) goes back to the environment.

Every value is checked here before it reaches a command line. The daemons are
started without a shell, so a value cannot run a command, but a path that is
not a device or a sysfs file must still never reach them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .const import FAN_CMD_NOOP, SETTINGS_FILE
from .storage import write_atomic

_LOGGER = logging.getLogger(__name__)

VERBOSITIES = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR")
COMMAND_TYPES = ("ata", "scsi")
# curve: hddfancontrol drives the fans between the two temperatures.
# auto: the web UI keeps the hottest drive near a target (fancontrol.py).
CONTROL_MODES = ("curve", "auto")
# How alerts are sent to NOTIFY_URL (notify.py), and in which language.
NOTIFY_FORMATS = ("ntfy", "gotify", "json")
NOTIFY_LANGUAGES = ("en", "it")
MAX_URL_LENGTH = 500
_URL = re.compile(r"^https?://[^\s/?#]+[^\s]*$")

SOURCE_ENV = "environment"
SOURCE_FILE = "file"

# Path components: letters, digits and the punctuation found in
# /dev/disk/by-id names and sysfs paths. No "..", no whitespace.
_COMPONENT = r"[A-Za-z0-9_:+@,.-]+"
_DEV_PATH = re.compile(rf"^/dev(/{_COMPONENT})+$")
_SYS_PATH = re.compile(rf"^/sys(/{_COMPONENT})+$")
_INTERFACE = re.compile(r"^[a-z0-9]+$")
_INTERVAL = re.compile(r"^\s*(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs)?\s*$")
_INTERVAL_FACTORS = {"": 1, "s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60, "h": 3600, "hr": 3600, "hrs": 3600}
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")

MAX_INTERVAL_S = 3600
MAX_AVERAGE = 100
MAX_SPIN_DOWN_S = 86400
MAX_HISTORY_DAYS = 365
MAX_SMART_INTERVAL_H = 168
MIN_TEMP = 0.0
MAX_TEMP = 120.0


# ---------------------------
#   SettingsError
# ---------------------------
class SettingsError(ValueError):
    """One or more invalid values.

    Each problem names the field (``pwms.1.start``), a code that the web UI
    translates (``error.<code>``) and the values the message needs.
    """

    def __init__(self, problems: list[dict[str, Any]]) -> None:
        """Keep the problems and build a readable message for the logs."""
        self.problems = problems
        super().__init__("; ".join(f"{p['field']}: {p['code']}" for p in problems))


# ---------------------------
#   Pwm
# ---------------------------
@dataclass(frozen=True)
class Pwm:
    """A PWM file and the values at which its fan starts and stops."""

    path: str
    start: int
    stop: int
    # The /sys/devices path of the chip, to find the output again when the
    # hwmon number changes at a reboot (Hardware.locate).
    chip: str | None = None


# ---------------------------
#   Hwmon
# ---------------------------
@dataclass(frozen=True)
class Hwmon:
    """An extra hwmon temperature input, with an optional range."""

    path: str
    temp_min: float | None = None
    temp_max: float | None = None


# ---------------------------
#   Settings
# ---------------------------
@dataclass(frozen=True)
class Settings:
    """Everything the two daemons are started with.

    The defaults are the defaults of hddfancontrol, and hd-idle's own idle
    time for the spin down, except restore_fan_settings: when hddfancontrol
    stops, the fans get their previous settings back instead of running at
    full speed.
    """

    drives: tuple[str, ...] = ()
    monitor_drives: tuple[str, ...] = ()
    pwms: tuple[Pwm, ...] = ()
    drive_temp_min: float = 30.0
    drive_temp_max: float = 50.0
    min_fan_speed_prct: int = 20
    interval_s: int = 20
    average: int = 1
    hwmons: tuple[Hwmon, ...] = ()
    restore_fan_settings: bool = True
    verbosity: str = "INFO"
    spin_down_time_s: int = 600
    spin_down_command: str = "ata"
    spin_down_drives: dict[str, int] = field(default_factory=dict)
    control_mode: str = "curve"
    target_temp: float = 40.0
    history_days: int = 30
    smart_interval_h: int = 6
    notify_url: str = ""
    notify_format: str = "ntfy"
    notify_language: str = "en"

    @property
    def complete(self) -> bool:
        """Whether the fans can be controlled: it needs drives and fans."""
        return bool(self.drives) and bool(self.pwms)

    def spin_down_of(self, drive: str) -> int:
        """Spin down time of a drive: its own, or the common one."""
        return self.spin_down_drives.get(drive, self.spin_down_time_s)

    def to_dict(self) -> dict[str, Any]:
        """Return the settings as JSON-ready data, the shape from_dict reads."""
        return asdict(self)


# ---------------------------
#   _Collector
# ---------------------------
class _Collector:
    """Collect every problem of a document instead of stopping at the first."""

    def __init__(self) -> None:
        """Start with no problems."""
        self.problems: list[dict[str, Any]] = []

    def add(self, field_name: str, code: str, **params: Any) -> None:
        """Record a problem."""
        self.problems.append({"field": field_name, "code": code, "params": params})

    def has(self, *fields: str) -> bool:
        """Whether one of these fields has a problem already."""
        return any(p["field"] in fields for p in self.problems)

    def integer(self, data: Mapping[str, Any], key: str, default: int, low: int, high: int, prefix: str = "") -> int:
        """Read an integer between low and high, both included. A default out of range means required."""
        name = prefix + key
        if key not in data and not low <= default <= high:
            self.add(name, "required")
            return default
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            self.add(name, "invalid_number")
            return default
        try:
            number = int(value)
        except ValueError:
            self.add(name, "invalid_number")
            return default
        if not low <= number <= high:
            self.add(name, "out_of_range", min=low, max=high)
            return default
        return number

    def number(self, data: Mapping[str, Any], key: str, default: float | None, prefix: str = "") -> float | None:
        """Read a temperature in °C, or None when it is missing and may be."""
        value = data.get(key, default)
        name = prefix + key
        if value is None:
            if default is not None:
                self.add(name, "required")
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            self.add(name, "invalid_number")
            return default
        try:
            number = float(value)
        except ValueError:
            self.add(name, "invalid_number")
            return default
        if not MIN_TEMP <= number <= MAX_TEMP:
            self.add(name, "out_of_range", min=MIN_TEMP, max=MAX_TEMP)
            return default
        return number

    def choice(self, data: Mapping[str, Any], key: str, default: str, choices: tuple[str, ...]) -> str:
        """Read one of a fixed set of strings."""
        value = data.get(key, default)
        if value not in choices:
            self.add(key, "invalid_choice", choices=", ".join(choices))
            return default
        return str(value)

    def boolean(self, data: Mapping[str, Any], key: str, default: bool) -> bool:
        """Read a boolean, from JSON or from an environment string."""
        value = data.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE:
            return True
        if isinstance(value, str) and value.strip().lower() in _FALSE:
            return False
        self.add(key, "invalid_bool")
        return default

    def items(self, data: Mapping[str, Any], key: str) -> list[Any]:
        """Read a list, which may be missing."""
        value = data.get(key, [])
        if not isinstance(value, (list, tuple)):
            self.add(key, "invalid_list")
            return []
        return list(value)


# ---------------------------
#   is_dev_path
# ---------------------------
def is_dev_path(value: str) -> bool:
    """Whether the value is a path under /dev without '..'."""
    return bool(_DEV_PATH.match(value)) and ".." not in value.split("/")


# ---------------------------
#   is_sys_path
# ---------------------------
def is_sys_path(value: str) -> bool:
    """Whether the value is a path under /sys without '..'."""
    return bool(_SYS_PATH.match(value)) and ".." not in value.split("/")


# ---------------------------
#   from_dict
# ---------------------------
def from_dict(data: Any) -> Settings:
    """Check a settings document and build the settings.

    Raises SettingsError with every problem found.
    """
    if not isinstance(data, Mapping):
        raise SettingsError([{"field": "", "code": "invalid_document", "params": {}}])
    defaults = Settings()
    check = _Collector()

    drives: list[str] = []
    for index, drive in enumerate(check.items(data, "drives")):
        name = f"drives.{index}"
        if not isinstance(drive, str) or not (is_dev_path(drive) or _INTERFACE.match(drive)):
            check.add(name, "invalid_drive")
        elif drive in drives:
            check.add(name, "duplicate")
        else:
            drives.append(drive)

    # The drives whose SMART data is read and shown. Without the key (an
    # environment without MONITOR_DRIVES), the drives that drive the fans.
    monitor_drives: list[str] = []
    if "monitor_drives" not in data:
        monitor_drives = [drive for drive in drives if drive.startswith("/")]
    for index, drive in enumerate(check.items(data, "monitor_drives")):
        name = f"monitor_drives.{index}"
        if not isinstance(drive, str) or not is_dev_path(drive):
            check.add(name, "invalid_dev_path")
        elif drive in monitor_drives:
            check.add(name, "duplicate")
        else:
            monitor_drives.append(drive)

    pwms: list[Pwm] = []
    for index, item in enumerate(check.items(data, "pwms")):
        prefix = f"pwms.{index}."
        if not isinstance(item, Mapping):
            check.add(f"pwms.{index}", "invalid_document")
            continue
        path = item.get("path")
        if not isinstance(path, str) or not is_sys_path(path):
            check.add(prefix + "path", "invalid_sys_path")
            continue
        if any(p.path == path for p in pwms):
            check.add(prefix + "path", "duplicate")
            continue
        start = check.integer(item, "start", -1, 0, 255, prefix)
        stop = check.integer(item, "stop", -1, 0, 255, prefix)
        chip = item.get("chip")
        if chip is not None and (not isinstance(chip, str) or not is_sys_path(chip)):
            check.add(prefix + "chip", "invalid_sys_path")
            chip = None
        if start >= 0 and stop >= 0:
            pwms.append(Pwm(path, start, stop, chip))

    temp_min = check.number(data, "drive_temp_min", defaults.drive_temp_min)
    temp_max = check.number(data, "drive_temp_max", defaults.drive_temp_max)
    temps_ok = not check.has("drive_temp_min", "drive_temp_max")
    if temps_ok and temp_min is not None and temp_max is not None and temp_min >= temp_max:
        check.add("drive_temp_max", "range_order")

    hwmons: list[Hwmon] = []
    for index, item in enumerate(check.items(data, "hwmons")):
        prefix = f"hwmons.{index}."
        if not isinstance(item, Mapping):
            check.add(f"hwmons.{index}", "invalid_document")
            continue
        path = item.get("path")
        if not isinstance(path, str) or not is_sys_path(path):
            check.add(prefix + "path", "invalid_sys_path")
            continue
        low = check.number(item, "temp_min", None, prefix)
        high = check.number(item, "temp_max", None, prefix)
        if (low is None) != (high is None):
            check.add(prefix + "temp_max", "range_both")
        elif low is not None and high is not None and low >= high:
            check.add(prefix + "temp_max", "range_order")
        hwmons.append(Hwmon(path, low, high))

    spin_down_drives: dict[str, int] = {}
    overrides = data.get("spin_down_drives", {})
    if not isinstance(overrides, Mapping):
        check.add("spin_down_drives", "invalid_document")
    else:
        for drive in overrides:
            if drive not in drives or not drive.startswith("/"):
                check.add(f"spin_down_drives.{drive}", "unknown_drive")
                continue
            value = check.integer(overrides, drive, -1, 0, MAX_SPIN_DOWN_S, "spin_down_drives.")
            if value >= 0:
                spin_down_drives[drive] = value

    target = check.number(data, "target_temp", defaults.target_temp)
    if temps_ok and not check.has("target_temp") and target is not None and temp_max is not None and target >= temp_max:
        check.add("target_temp", "target_order")

    settings = Settings(
        drives=tuple(drives),
        monitor_drives=tuple(monitor_drives),
        pwms=tuple(pwms),
        drive_temp_min=temp_min if temp_min is not None else defaults.drive_temp_min,
        drive_temp_max=temp_max if temp_max is not None else defaults.drive_temp_max,
        min_fan_speed_prct=check.integer(data, "min_fan_speed_prct", defaults.min_fan_speed_prct, 0, 100),
        interval_s=check.integer(data, "interval_s", defaults.interval_s, 1, MAX_INTERVAL_S),
        average=check.integer(data, "average", defaults.average, 1, MAX_AVERAGE),
        hwmons=tuple(hwmons),
        restore_fan_settings=check.boolean(data, "restore_fan_settings", defaults.restore_fan_settings),
        verbosity=check.choice(data, "verbosity", defaults.verbosity, VERBOSITIES),
        spin_down_time_s=check.integer(data, "spin_down_time_s", defaults.spin_down_time_s, 0, MAX_SPIN_DOWN_S),
        spin_down_command=check.choice(data, "spin_down_command", defaults.spin_down_command, COMMAND_TYPES),
        spin_down_drives=spin_down_drives,
        control_mode=check.choice(data, "control_mode", defaults.control_mode, CONTROL_MODES),
        target_temp=target if target is not None else defaults.target_temp,
        history_days=check.integer(data, "history_days", defaults.history_days, 1, MAX_HISTORY_DAYS),
        smart_interval_h=check.integer(data, "smart_interval_h", defaults.smart_interval_h, 0, MAX_SMART_INTERVAL_H),
        notify_url=_notify_url(check, data.get("notify_url", defaults.notify_url)),
        notify_format=check.choice(data, "notify_format", defaults.notify_format, NOTIFY_FORMATS),
        notify_language=check.choice(data, "notify_language", defaults.notify_language, NOTIFY_LANGUAGES),
    )
    if check.problems:
        raise SettingsError(check.problems)
    return settings


# ---------------------------
#   _notify_url
# ---------------------------
def _notify_url(check: _Collector, value: Any) -> str:
    """Read the address alerts are sent to: empty (none), or an http(s) URL."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH or not _URL.match(value.strip()):
        check.add("notify_url", "invalid_url")
        return ""
    return value.strip()


# ---------------------------
#   _parse_interval
# ---------------------------
def _parse_interval(value: str) -> int | str:
    """Turn '10s', '3min' or '30' into seconds, or leave the text for from_dict to refuse."""
    match = _INTERVAL.match(value)
    if not match:
        return value
    return int(match.group(1)) * _INTERVAL_FACTORS[match.group(2) or ""]


# ---------------------------
#   env_document
# ---------------------------
def env_document(env: Mapping[str, str]) -> dict[str, Any]:
    """Turn the environment variables of the upstream image into a settings document.

    Variables that are not set keep the defaults. from_dict checks the result.
    """
    doc: dict[str, Any] = {}
    if env.get("DRIVES", "").strip():
        doc["drives"] = env["DRIVES"].split()
    if "MONITOR_DRIVES" in env:
        doc["monitor_drives"] = env["MONITOR_DRIVES"].split()
    if env.get("PWM", "").strip():
        pwms: list[Any] = []
        for token in env["PWM"].split():
            # Same split as hddfancontrol: the path may hold ':'.
            parts = token.rsplit(":", 2)
            if len(parts) == 3:
                pwms.append({"path": parts[0], "start": parts[1], "stop": parts[2]})
            else:
                pwms.append({"path": token})
        doc["pwms"] = pwms
    if env.get("DRIVE_TEMP_RANGE", "").strip():
        values = env["DRIVE_TEMP_RANGE"].split()
        doc["drive_temp_min"] = values[0]
        doc["drive_temp_max"] = values[1] if len(values) > 1 else None
    if env.get("HWMONS", "").strip():
        hwmons: list[Any] = []
        for token in env["HWMONS"].split():
            parts = token.split(":")
            hwmon: dict[str, Any] = {"path": parts[0]}
            if len(parts) > 1:
                hwmon["temp_min"] = parts[1]
                hwmon["temp_max"] = parts[2] if len(parts) > 2 else None
            hwmons.append(hwmon)
        doc["hwmons"] = hwmons
    if env.get("INTERVAL", "").strip():
        doc["interval_s"] = _parse_interval(env["INTERVAL"])
    simple = {
        "MIN_FAN_SPEED_PRCT": "min_fan_speed_prct",
        "AVERAGE": "average",
        "RESTORE_FAN_SETTINGS": "restore_fan_settings",
        "SPIN_DOWN_TIME_S": "spin_down_time_s",
        "SPIN_DOWN_COMMAND": "spin_down_command",
        "TARGET_TEMP": "target_temp",
        "HISTORY_DAYS": "history_days",
        "SMART_INTERVAL_H": "smart_interval_h",
        "NOTIFY_URL": "notify_url",
    }
    for var, key in simple.items():
        if env.get(var, "").strip():
            doc[key] = env[var].strip()
    if env.get("CONTROL_MODE", "").strip():
        doc["control_mode"] = env["CONTROL_MODE"].strip().lower()
    for var, key in {"NOTIFY_FORMAT": "notify_format", "NOTIFY_LANGUAGE": "notify_language"}.items():
        if env.get(var, "").strip():
            doc[key] = env[var].strip().lower()
    if env.get("VERBOSITY", "").strip():
        doc["verbosity"] = env["VERBOSITY"].strip().upper()
    return doc


# ---------------------------
#   hddfancontrol_args
# ---------------------------
def hddfancontrol_args(settings: Settings, temp_log: Path | None, measure_only: bool = False) -> list[str]:
    """Build the arguments of 'hddfancontrol daemon', without the program.

    With measure_only, hddfancontrol leaves the fans alone: it calls
    /bin/true instead of writing the PWM files, and only probes the drives
    and writes the temperature log, for the auto mode and manual control.
    """
    args = ["-v", settings.verbosity, "daemon", "--drives", *settings.drives]
    if measure_only:
        args += ["--fan-cmd", FAN_CMD_NOOP]
    else:
        args += ["--pwm", *(f"{p.path}:{p.start}:{p.stop}" for p in settings.pwms)]
    args += ["--drive-temp-range", f"{settings.drive_temp_min:g}", f"{settings.drive_temp_max:g}"]
    args += ["--min-fan-speed-prct", str(settings.min_fan_speed_prct)]
    args += ["--interval", f"{settings.interval_s}s"]
    args += ["--average", str(settings.average)]
    for hwmon in settings.hwmons:
        if hwmon.temp_min is not None and hwmon.temp_max is not None:
            args += ["--hwmons", f"{hwmon.path}:{hwmon.temp_min:g}:{hwmon.temp_max:g}"]
        else:
            args += ["--hwmons", hwmon.path]
    if settings.restore_fan_settings:
        args.append("--restore-fan-settings")
    if temp_log is not None:
        args += ["--temp-log", str(temp_log)]
    return args


# ---------------------------
#   hdidle_args
# ---------------------------
def hdidle_args(settings: Settings) -> list[str] | None:
    """Build the arguments of hd-idle, or None when it must not run.

    hd-idle watches every disk of the system with its default idle time, so
    the default is set to 0 and only the chosen drives get a spin down time,
    their own or the common one. A drive with 0 never spins down. Drives
    given as an interface name ('ata') are not files, and hd-idle cannot use
    them.
    """
    drives = [(d, settings.spin_down_of(d)) for d in settings.drives if d.startswith("/")]
    drives = [(d, seconds) for d, seconds in drives if seconds > 0]
    if not drives:
        return None
    args = ["-i", "0"]
    if settings.verbosity in ("TRACE", "DEBUG"):
        args.append("-d")
    for drive, seconds in drives:
        args += ["-a", drive, "-i", str(seconds), "-c", settings.spin_down_command]
    return args


# ---------------------------
#   SettingsStore
# ---------------------------
class SettingsStore:
    """Load the settings from settings.json or the environment, and save them."""

    def __init__(self, config_dir: Path, env: Mapping[str, str] | None = None) -> None:
        """Remember where the file is and which environment to fall back on."""
        self.path = config_dir / SETTINGS_FILE
        self._env = dict(os.environ if env is None else env)
        self.error: SettingsError | None = None

    def load(self) -> tuple[Settings, str]:
        """Return the settings in force and where they come from.

        A file that cannot be read or holds invalid values is reported in
        ``error`` and the environment is used, so a bad file never stops the
        fans from being controlled.
        """
        self.error = None
        if self.path.exists():
            try:
                with self.path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
                return from_dict(data), SOURCE_FILE
            except (OSError, json.JSONDecodeError) as err:
                _LOGGER.error("Cannot read %s, using the environment: %s", self.path, err)
                self.error = SettingsError([{"field": "", "code": "unreadable_file", "params": {}}])
            except SettingsError as err:
                _LOGGER.error("Invalid values in %s, using the environment: %s", self.path, err)
                self.error = err
        return self.from_env(), SOURCE_ENV

    def from_env(self) -> Settings:
        """Return the settings of the environment, or the defaults if they are invalid."""
        try:
            return from_dict(env_document(self._env))
        except SettingsError as err:
            _LOGGER.error("Invalid environment variables, using the defaults: %s", err)
            self.error = err
            return Settings()

    def save(self, settings: Settings) -> None:
        """Write the file in one step, so a crash never leaves half a file. Blocking."""
        write_atomic(self.path, json.dumps(settings.to_dict(), indent=2) + "\n")
        self.error = None

    def reset(self) -> None:
        """Delete the file, so the environment is used again."""
        self.path.unlink(missing_ok=True)
        self.error = None
