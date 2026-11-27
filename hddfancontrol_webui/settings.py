"""Settings of hddfancontrol and hd-idle.

The settings come from the environment variables of the container until the
web UI saves them to
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
from urllib.parse import urlsplit, urlunsplit

from .const import FAN_CMD_NOOP, SETTINGS_FILE, TRUE_WORDS
from .cron import CronError, parse
from .notify import forbidden_host
from .storage import write_atomic

_LOGGER = logging.getLogger(__name__)

VERBOSITIES = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR")
COMMAND_TYPES = ("ata", "scsi")
# curve: hddfancontrol drives the fans between the two temperatures.
# auto: the web UI keeps the hottest drive near a target (fancontrol.py).
CONTROL_MODES = ("curve", "auto")
# How alerts are sent to NOTIFY_URL (notify.py), and in which language.
NOTIFY_FORMATS = ("ntfy", "gotify", "json")
NOTIFY_LANGUAGES = ("en", "it", "es", "fr", "de", "pt", "ru", "ja", "zh", "hi", "ar")
MAX_REPEAT_H = 168
MAX_URL_LENGTH = 500
# The host, then nothing or a path, query or fragment: no overlap, so no slow backtracking.
# What replaces the secrets of notify_url in every answer of the API (mask_notify_url).
NOTIFY_MASK = "•••"
# Every pattern of a path or a name is used with fullmatch (or ends in \Z): "$" would
# also match before a final newline, and "/dev/sda\n" would reach a command line.
_URL = re.compile(r"https?://[^\s/?#]+(?:[/?#]\S*)?")

SOURCE_ENV = "environment"
SOURCE_FILE = "file"

# Path components: letters, digits and the punctuation found in
# /dev/disk/by-id names and sysfs paths. No "..", no whitespace.
_COMPONENT = r"[A-Za-z0-9_:+@,.-]+"
_DEV_PATH = re.compile(rf"/dev(/{_COMPONENT})+")
_SYS_PATH = re.compile(rf"/sys(/{_COMPONENT})+")
_INTERFACE = re.compile(r"[a-z0-9]+")
# What the fan controller writes as root: only a PWM output of a hwmon chip,
# never another /sys file (/sys/block/sda/device/delete removes a drive).
_PWM_PATH = re.compile(r"/sys/class/hwmon/hwmon[0-9]+/pwm[0-9]+")
# A drive: its stable names (by-id, by-path) or the kernel's name of a
# whole SATA/SAS or NVMe drive. They go to smartctl, hddfancontrol (hdparm)
# and hd-idle as root: no other /dev node (a partition table, /dev/mem).
_DRIVE_PATH = re.compile(rf"/dev/(?:disk/by-(?:id|path)/{_COMPONENT}|sd[a-z]+|nvme[0-9]+n[0-9]+)")
# What hddfancontrol reads: a temperature input of a hwmon chip.
_TEMP_INPUT = re.compile(r"/temp[0-9]+_input\Z")
# A temperature of HWMONS (path:min:max), as hddfancontrol reads it: a number, maybe negative or with decimals.
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
# Where the chips of the fans live (Hardware.chip_of).
_CHIP_PREFIX = "/sys/devices/"
_INTERVAL = re.compile(r"\s*([0-9]{1,9})\s*(s|sec|secs|m|min|mins|h|hr|hrs)?\s*")
_INTERVAL_FACTORS = {"": 1, "s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60, "h": 3600, "hr": 3600, "hrs": 3600}
_FALSE = ("0", "false", "no", "off", "")

MAX_INTERVAL_S = 3600
MAX_AVERAGE = 100
MAX_SPIN_DOWN_S = 86400
MAX_HISTORY_DAYS = 365
MIN_TEMP = 0.0
# Extra hwmon sensors (a CPU runs to 100 °C).
MAX_TEMP = 120.0
# Drive temperatures: no drive is meant to run hotter. The own limits of a
# drive that is not NVMe stop lower: SATA/SAS drives are rated to 60-70 °C.
MAX_DRIVE_TEMP = 80.0
MAX_SATA_TEMP = 70.0


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
    # The drives this fan follows (a group of fans per set of drives); empty
    # means every drive.
    drives: tuple[str, ...] = ()


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
    # The schedules are cron expressions (cron.py), in local time; empty: never.
    # SMART data is read at these times (empty: only on demand).
    smart_schedule: str = "0 */6 * * *"
    notify_url: str = ""
    notify_format: str = "ntfy"
    notify_language: str = "en"
    # An alert still on is sent again every this many hours (0: once).
    notify_repeat_h: int = 0
    # Quiet hours: every minute the expression matches ("* 22-23,0-6 * * *").
    # Only the urgent alerts are sent then, the rest when they end.
    notify_quiet: str = ""
    # When a summary of the drives and fans goes out with the alerts.
    notify_summary: str = ""
    # When the SMART self-tests start, on every monitored drive.
    selftest_short: str = ""
    selftest_long: str = ""
    # Limits of single drives over the common ones: {drive: {"min", "target", "max"}}, each optional.
    drive_limits: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Whether the fans can be controlled: it needs drives and fans."""
        return bool(self.drives) and bool(self.pwms)

    def limits_of(self, drive: str) -> tuple[float, float, float]:
        """Minimum, target and maximum temperature of a drive: its own, or the common ones."""
        own = self.drive_limits.get(drive, {})
        return (
            own.get("min", self.drive_temp_min),
            own.get("target", self.target_temp),
            own.get("max", self.drive_temp_max),
        )

    def spin_down_of(self, drive: str) -> int:
        """Spin down time of a drive: its own, or the common one."""
        return self.spin_down_drives.get(drive, self.spin_down_time_s)

    def to_dict(self) -> dict[str, Any]:
        """Return the settings as JSON-ready data, the shape from_dict reads."""
        return asdict(self)

    def masked_dict(self) -> dict[str, Any]:
        """to_dict with notify_url masked (mask_notify_url): what leaves the core."""
        data = self.to_dict()
        data["notify_url"] = mask_notify_url(self.notify_url, self.notify_format)
        return data


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

    def number(
        self, data: Mapping[str, Any], key: str, default: float | None, prefix: str = "", high: float = MAX_TEMP
    ) -> float | None:
        """Read a temperature in °C up to high, or None when it is missing and may be."""
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
        except (ValueError, OverflowError):  # an int too large for a float
            self.add(name, "invalid_number")
            return default
        if not MIN_TEMP <= number <= high:
            self.add(name, "out_of_range", min=MIN_TEMP, max=high)
            return default
        return number

    def choice(self, data: Mapping[str, Any], key: str, default: str, choices: tuple[str, ...]) -> str:
        """Read one of a fixed set of strings."""
        value = data.get(key, default)
        if value not in choices:
            self.add(key, "invalid_choice", choices=", ".join(choices))
            return default
        return str(value)

    def cron(self, data: Mapping[str, Any], key: str, default: str) -> str:
        """Read a cron expression, or empty for never."""
        value = data.get(key, default)
        if value is None or (isinstance(value, str) and not value.strip()):
            return ""
        if not isinstance(value, str):
            self.add(key, "cron_fields")
            return default
        text = " ".join(value.split())
        try:
            parse(text)
        except CronError as err:
            self.add(key, err.code, **err.params)
            return default
        return text

    def boolean(self, data: Mapping[str, Any], key: str, default: bool) -> bool:
        """Read a boolean, from JSON or from an environment string."""
        value = data.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in TRUE_WORDS:
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
    return bool(_DEV_PATH.fullmatch(value)) and ".." not in value.split("/")


# ---------------------------
#   is_sys_path
# ---------------------------
def is_sys_path(value: str) -> bool:
    """Whether the value is a path under /sys without '..'."""
    return bool(_SYS_PATH.fullmatch(value)) and ".." not in value.split("/")


# ---------------------------
#   is_drive_path
# ---------------------------
def is_drive_path(value: str) -> bool:
    """Whether the value names a whole drive: /dev/disk/by-id/…, /dev/disk/by-path/…, /dev/sdX or /dev/nvmeNnN."""
    return is_dev_path(value) and bool(_DRIVE_PATH.fullmatch(value))


# ---------------------------
#   is_pwm_path
# ---------------------------
def is_pwm_path(value: str) -> bool:
    """Whether the value is a PWM output of a hwmon chip: /sys/class/hwmon/hwmonN/pwmN."""
    return bool(_PWM_PATH.fullmatch(value))


# ---------------------------
#   is_temp_input
# ---------------------------
def is_temp_input(value: str) -> bool:
    """Whether the value is a temperature input under /sys: a path ending in /tempN_input."""
    return is_sys_path(value) and bool(_TEMP_INPUT.search(value))


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
        if not isinstance(drive, str) or not (is_drive_path(drive) or _INTERFACE.fullmatch(drive)):
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
        if not isinstance(drive, str) or not is_drive_path(drive):
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
        if not isinstance(path, str) or not is_pwm_path(path):
            check.add(prefix + "path", "invalid_pwm_path")
            continue
        if any(p.path == path for p in pwms):
            check.add(prefix + "path", "duplicate")
            continue
        start = check.integer(item, "start", -1, 0, 255, prefix)
        stop = check.integer(item, "stop", -1, 0, 255, prefix)
        chip = item.get("chip")
        if chip is not None and (not isinstance(chip, str) or not is_sys_path(chip) or not chip.startswith(_CHIP_PREFIX)):
            check.add(prefix + "chip", "invalid_chip")
            chip = None
        followed: list[str] = []
        for number, drive in enumerate(check.items(item, "drives")):
            if not isinstance(drive, str) or drive not in drives or not drive.startswith("/"):
                check.add(f"{prefix}drives.{number}", "unknown_drive")
            elif drive not in followed:
                followed.append(drive)
        if start >= 0 and stop >= 0:
            pwms.append(Pwm(path, start, stop, chip, tuple(followed)))

    temp_min = check.number(data, "drive_temp_min", defaults.drive_temp_min, high=MAX_DRIVE_TEMP)
    temp_max = check.number(data, "drive_temp_max", defaults.drive_temp_max, high=MAX_DRIVE_TEMP)
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
        if not isinstance(path, str) or not is_temp_input(path):
            check.add(prefix + "path", "invalid_temp_path")
            continue
        if ":" in path:
            # hddfancontrol cuts --hwmons at the first ':' (path:min:max): use /sys/class/hwmon/hwmonN/tempN_input.
            check.add(prefix + "path", "temp_path_colon")
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

    target = check.number(data, "target_temp", defaults.target_temp, high=MAX_DRIVE_TEMP)
    if temps_ok and not check.has("target_temp") and target is not None and temp_max is not None and target >= temp_max:
        check.add("target_temp", "target_order")

    drive_limits = _drive_limits(check, data.get("drive_limits", {}), drives, temp_min, target, temp_max)

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
        smart_schedule=check.cron(_smart_schedule(data), "smart_schedule", defaults.smart_schedule),
        notify_url=_notify_url(check, data.get("notify_url", defaults.notify_url)),
        notify_format=check.choice(data, "notify_format", defaults.notify_format, NOTIFY_FORMATS),
        notify_language=check.choice(data, "notify_language", defaults.notify_language, NOTIFY_LANGUAGES),
        notify_repeat_h=check.integer(data, "notify_repeat_h", defaults.notify_repeat_h, 0, MAX_REPEAT_H),
        notify_quiet=check.cron(data, "notify_quiet", defaults.notify_quiet),
        notify_summary=check.cron(data, "notify_summary", defaults.notify_summary),
        selftest_short=check.cron(data, "selftest_short", defaults.selftest_short),
        selftest_long=check.cron(data, "selftest_long", defaults.selftest_long),
        drive_limits=drive_limits,
    )
    if check.problems:
        raise SettingsError(check.problems)
    return settings


# ---------------------------
#   _drive_limits
# ---------------------------
def _drive_limits(
    check: _Collector, value: Any, drives: list[str], low: float | None, target: float | None, high: float | None
) -> dict[str, dict[str, float]]:
    """Read the limits of single drives; each falls back to the common one, and they must stay in order."""
    if not isinstance(value, Mapping):
        check.add("drive_limits", "invalid_document")
        return {}
    limits: dict[str, dict[str, float]] = {}
    for drive, item in value.items():
        name = f"drive_limits.{drive}"
        if drive not in drives or not str(drive).startswith("/"):
            check.add(name, "unknown_drive")
            continue
        if not isinstance(item, Mapping):
            check.add(name, "invalid_document")
            continue
        own: dict[str, float] = {}
        high_of_drive = MAX_DRIVE_TEMP if "nvme" in str(drive).rsplit("/", 1)[-1] else MAX_SATA_TEMP
        for key in ("min", "target", "max"):
            number = check.number(item, key, None, name + ".", high=high_of_drive)
            if number is not None:
                own[key] = number
        if not own:
            continue
        mine_low = own.get("min", low)
        mine_target = own.get("target", target)
        mine_high = own.get("max", high)
        if mine_low is not None and mine_high is not None and mine_low >= mine_high:
            check.add(name + ".max", "range_order")
        # A target is checked only when the drive has its own: with only its own
        # maximum under the common target, the auto mode simply runs full speed sooner.
        elif "target" in own and mine_target is not None and mine_high is not None and mine_target >= mine_high:
            check.add(name + ".target", "target_order")
        limits[drive] = own
    return limits


# ---------------------------
#   _smart_schedule
# ---------------------------
def _smart_schedule(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Turn smart_interval_h (hours, up to 0.6) into smart_schedule, when only the old key is there.

    Every N hours from midnight ("0 */N * * *"), every N/24 days, or weekly:
    the same as before when N divides 24, close to it otherwise. 0 (only on
    demand) becomes empty.
    """
    if "smart_schedule" in data or "smart_interval_h" not in data:
        return data
    hours = data["smart_interval_h"]
    try:
        number = int(hours)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an infinite float (1e400 in JSON)
        return {"smart_schedule": hours}
    if number <= 0:
        return {"smart_schedule": ""}
    if number < 24:
        return {"smart_schedule": "0 * * * *" if number == 1 else f"0 */{number} * * *"}
    if number < 168:
        return {"smart_schedule": "0 0 * * *" if number < 48 else f"0 0 */{number // 24} * *"}
    return {"smart_schedule": "0 0 * * 0"}


# ---------------------------
#   mask_notify_url
# ---------------------------
def mask_notify_url(url: str, fmt: str) -> str:
    """The address with its secrets replaced by NOTIFY_MASK: what the API shows.

    Masked: the user and password, the value of each query parameter (the
    token of Gotify), the fragment, for ntfy the topic (the last part of
    the path), which anyone who knows it can read, and for a JSON webhook
    the whole path, where Slack and Discord keep their token. Shown: the
    scheme, host and port, and the path of ntfy before its topic and of
    Gotify (/message). The same address and format
    always give the same mask, so restore_notify_url can recognise it.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = NOTIFY_MASK + "@" + netloc.rsplit("@", 1)[1]
    path = parts.path
    if fmt == "ntfy" and path.strip("/"):
        head, _, _ = path.rstrip("/").rpartition("/")
        path = f"{head}/{NOTIFY_MASK}"
    elif fmt == "json" and path.strip("/"):
        # A webhook keeps its token in the path (Slack, Discord): none of it is shown.
        path = f"/{NOTIFY_MASK}"
    query = "&".join(
        f"{key}={NOTIFY_MASK}" if sep else NOTIFY_MASK
        for key, sep, _ in (item.partition("=") for item in parts.query.split("&") if item)
    )
    fragment = NOTIFY_MASK if parts.fragment else ""
    return urlunsplit((parts.scheme, netloc, path, query, fragment))


# ---------------------------
#   restore_notify_url
# ---------------------------
def restore_notify_url(data: Any, current: Settings) -> Any:
    """A settings document with a masked notify_url put back to the address in force; raise SettingsError for a broken one.

    The page shows the masked address and sends it back when it is not
    changed: that keeps current.notify_url (compared with its mask in the
    format in force, the one the page got). Any other value replaces the
    address, an empty one clears it. A value that holds NOTIFY_MASK but is
    not that mask (edited by hand, another installation's backup) would
    store a broken address: notify_url_masked.
    """
    if not isinstance(data, dict) or not isinstance(data.get("notify_url"), str) or NOTIFY_MASK not in data["notify_url"]:
        return data
    if data["notify_url"].strip() != mask_notify_url(current.notify_url, current.notify_format):
        raise SettingsError([{"field": "notify_url", "code": "notify_url_masked", "params": {}}])
    return {**data, "notify_url": current.notify_url}


# ---------------------------
#   _notify_url
# ---------------------------
def _notify_url(check: _Collector, value: Any) -> str:
    """Read the address alerts are sent to: empty (none), or an http(s) URL.

    A host that is the NAS itself or a link-local, unspecified or multicast
    address, written as such (notify.forbidden_host), is refused at once; a
    name that resolves to one is refused when a message is sent.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH or not _URL.fullmatch(value.strip()):
        check.add("notify_url", "invalid_url")
        return ""
    try:
        host = urlsplit(value.strip()).hostname or ""
    except ValueError:
        check.add("notify_url", "invalid_url")
        return ""
    if forbidden_host(host):
        check.add("notify_url", "notify_address_forbidden")
        return ""
    return value.strip()


# ---------------------------
#   _parse_interval
# ---------------------------
def _parse_interval(value: str) -> int | str:
    """Turn '10s', '3min' or '30' into seconds, or leave the text for from_dict to refuse."""
    match = _INTERVAL.fullmatch(value)
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
            # path[:min[:max]], the numbers taken from the end: a path may hold ':'.
            path, numbers = token, list[str]()
            for _ in range(2):
                head, colon, tail = path.rpartition(":")
                if not colon or not _NUMBER.fullmatch(tail):
                    break
                path, numbers = head, [tail, *numbers]
            hwmon: dict[str, Any] = {"path": path}
            if numbers:
                hwmon["temp_min"] = numbers[0]
                hwmon["temp_max"] = numbers[1] if len(numbers) > 1 else None
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
        "SMART_SCHEDULE": "smart_schedule",
        "NOTIFY_URL": "notify_url",
        "NOTIFY_REPEAT_H": "notify_repeat_h",
        "NOTIFY_QUIET": "notify_quiet",
        "NOTIFY_SUMMARY": "notify_summary",
        "SELFTEST_SHORT": "selftest_short",
        "SELFTEST_LONG": "selftest_long",
    }
    for var, key in simple.items():
        if env.get(var, "").strip():
            doc[key] = env[var].strip()
    if env.get("CONTROL_MODE", "").strip():
        doc["control_mode"] = env["CONTROL_MODE"].strip().lower()
    for var, key in {
        "NOTIFY_FORMAT": "notify_format",
        "NOTIFY_LANGUAGE": "notify_language",
    }.items():
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

        A file that cannot be read (whatever the reason: not UTF-8, nested
        too deep) or holds invalid values is reported in ``error`` and the
        environment is used, so a bad file never stops the fans from being
        controlled.
        """
        self.error = None
        if self.path.exists():
            try:
                with self.path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
                return from_dict(data), SOURCE_FILE
            except SettingsError as err:
                _LOGGER.error("Invalid values in %s, using the environment: %s", self.path, err)
                self.error = err
            except Exception as err:  # not UTF-8, nested too deep, a bug: the fans come first
                _LOGGER.error("Cannot read %s, using the environment: %r", self.path, err)
                self.error = SettingsError([{"field": "", "code": "unreadable_file", "params": {}}])
        return self.from_env(), SOURCE_ENV

    def from_env(self) -> Settings:
        """Return the settings of the environment, or the defaults if they are invalid."""
        try:
            return from_dict(env_document(self._env))
        except SettingsError as err:
            _LOGGER.error("Invalid environment variables, using the defaults: %s", err)
            self.error = err
        except Exception as err:  # a bug must not stop the start: the defaults control nothing
            _LOGGER.error("Cannot read the environment variables, using the defaults: %r", err)
            self.error = SettingsError([{"field": "", "code": "unreadable_env", "params": {}}])
        return Settings()

    def save(self, settings: Settings) -> None:
        """Write the file in one step, so a crash never leaves half a file. Blocking."""
        write_atomic(self.path, json.dumps(settings.to_dict(), indent=2) + "\n")
        self.error = None

    def reset(self) -> None:
        """Delete the file, so the environment is used again."""
        self.path.unlink(missing_ok=True)
        self.error = None
