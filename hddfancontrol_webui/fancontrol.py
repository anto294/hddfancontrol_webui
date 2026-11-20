"""Fan control done by the web UI itself: the curve and auto modes, and manual control.

hddfancontrol only measures: it runs with ``--fan-cmd /bin/true``, keeps
probing the drives without waking them, and writes the temperature log. This
module reads that log and writes the PWM files.

Each fan follows its own drives (Pwm.drives, every drive when empty), and
each drive has its own minimum, target and maximum (Settings.limits_of),
so one group of fans can cool the hot enterprise drives while another
follows the rest.

- Curve mode: like hddfancontrol, a drive asks for the minimum speed up to
  its minimum temperature, full speed from its maximum, and in proportion
  between. A drive must cool CURVE_COOLDOWN °C before it asks for less, and
  a fan slows down by at most MAX_DECREASE_PER_STEP per step, so a drive
  going back and forth by one degree no longer makes the fans pump.
- Auto mode: a PI controller per fan keeps the drive that is furthest above
  its target near it, with as little fan speed as that needs. It is
  feedback control, not machine learning.

Safety, in every mode:

- A drive at or above its maximum puts its fans at full speed (in manual
  control, every fan).
- A temperature log that stops updating, or hddfancontrol ending, puts
  every fan at full speed, since the temperatures are no longer known.
- A drive that turns null (asleep, or a failed probe) keeps counting with
  its last temperature for NULL_HOLD_INTERVALS intervals.
- An unexpected error in a loop puts every fan at full speed; the loop
  goes on, and the next step that works takes over again.
- When control ends, each PWM output gets back the pwmN_enable value it had,
  so the chip takes over again (or, with restore_fan_settings off, stays
  at full speed).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .settings import Hwmon, Pwm, Settings, is_pwm_path
from .storage import write_atomic

_LOGGER = logging.getLogger(__name__)

PWM_MAX = 255
ENABLE_MANUAL = 1
# Full speed for a drive's fans from these temperatures, whatever the
# settings: SATA/SAS drives are rated to 60-70 °C, NVMe drives run warmer
# and throttle past 80 °C.
DRIVE_CEILING = 65.0
NVME_CEILING = 80.0
# A sample timed further ahead than this is not trusted (the clock jumped back).
FUTURE_TOLERANCE_S = 60.0
# Changes at every boot: the state saved by PwmWriter holds for one boot only.
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

# Gains of the PI controller, per °C of error. The proportional part adds 8 %
# of speed per °C above the target; the integral part adds about 9 % per
# minute for each °C that stays above it. Drive temperatures move over
# minutes, so these stay calm and never oscillate in practice.
KP = 0.08
KI = 0.0015
# Slowing down is limited to this much speed per step, so a short dip does
# not stop the fans; speeding up is never limited.
MAX_DECREASE_PER_STEP = 0.10

# hddfancontrol logs null both for a sleeping drive and for a probe that
# failed (it tolerates 4 failures in a row, common under heavy I/O such as a
# scrub, and exits at the 5th). The last temperature of a drive that turns
# null is kept this many intervals, so a failed probe of a hot drive never
# lets the fans slow down; a drive that really sleeps drops out after it.
NULL_HOLD_INTERVALS = 5

# Curve mode: a drive must cool this much under the temperature it last asked
# for before its fans slow down; warming up counts at once.
CURVE_COOLDOWN = 2.0
# How often the stable names of the drives (by-id -> sda) are read again.
NAMES_REFRESH_S = 60.0

# Why manual control put every fan at full speed.
SAFETY_HOT = "hot"
SAFETY_STALE = "stale"
SAFETY_ERROR = "error"

LatestTemps = Callable[[], dict[str, Any] | None]
DriveNames = Callable[[], dict[str, str]]
Log = Callable[[str], None]


# ---------------------------
#   NotManual
# ---------------------------
class NotManual(RuntimeError):
    """A manual value came while manual control is not running (yet, or any more)."""


# ---------------------------
#   speed_to_pwm
# ---------------------------
def speed_to_pwm(speed: float, pwm: Pwm, running: bool) -> int:
    """Turn a speed from 0 to 1 into a PWM value, as hddfancontrol does.

    Speeds above 0 go from the stop value to 255. A fan that stands still
    gets at least its start value, because the stop value is too low to get
    it turning.
    """
    if speed <= 0:
        return 0
    speed = min(speed, 1.0)
    value = pwm.stop + round(speed * (PWM_MAX - pwm.stop))
    if not running:
        value = max(value, pwm.start)
    return min(value, PWM_MAX)


# ---------------------------
#   PiController
# ---------------------------
class PiController:
    """Keep a drive at a target temperature."""

    def __init__(self, target: float, maximum: float, min_speed: float) -> None:
        """Aim at target; maximum means full speed; never go below min_speed."""
        self.target = target
        self.maximum = maximum
        self.min_speed = min_speed
        # Start at full speed until the first temperatures say otherwise.
        self.integral = 1.0
        self.speed = 1.0

    def step(self, hottest: float | None, dt: float) -> float:
        """Compute the next speed from the hottest drive temperature.

        None means every drive sleeps: the fans go to the minimum speed.
        """
        if hottest is None:
            return self.step_error(None, False, dt)
        return self.step_error(hottest - self.target, hottest >= self.maximum, dt)

    def step_error(self, error: float | None, at_max: bool, dt: float) -> float:
        """Compute the next speed from how far (°C) the drive is above its target.

        at_max means a drive reached its maximum: full speed. None means
        there is no drive to follow (all asleep): the minimum speed.
        """
        if error is None:
            self.integral = self.min_speed
            self.speed = self.min_speed
            return self.speed
        if at_max:
            self.integral = 1.0
            self.speed = 1.0
            return self.speed
        proportional = KP * error
        unclamped = proportional + self.integral
        # Integrate only when it does not push further into a limit.
        if not ((unclamped >= 1.0 and error > 0) or (unclamped <= self.min_speed and error < 0)):
            self.integral = min(1.0, max(0.0, self.integral + KI * error * dt))
        speed = min(1.0, max(self.min_speed, proportional + self.integral))
        speed = max(speed, self.speed - MAX_DECREASE_PER_STEP)
        self.speed = speed
        return speed


# ---------------------------
#   curve_speed
# ---------------------------
def curve_speed(temp: float, low: float, high: float, min_speed: float) -> float:
    """The speed the curve asks for at temp: min_speed up to low, 1 from high, a line between."""
    if temp <= low:
        return min_speed
    if temp >= high:
        return 1.0
    return min_speed + (1.0 - min_speed) * (temp - low) / (high - low)


# ---------------------------
#   _drive_temps
# ---------------------------
def _drive_temps(latest: dict[str, Any], hwmon_count: int) -> list[tuple[str, float | None]]:
    """Return the drive entries of a sample.

    hddfancontrol logs every drive first, then every hwmon input, and an
    hwmon input is always there. So the drives are every value but the last
    hwmon_count, however many drives an interface name such as 'ata' gave.
    """
    items = list(latest["temps"].items())
    return items[: max(0, len(items) - hwmon_count)]


# ---------------------------
#   DriveTemps
# ---------------------------
class DriveTemps:
    """The temperature of each drive, averaged, keeping for a while the last one of a drive that turned null.

    per_drive gives the averages, for the speed; raw then holds the last
    reading of each drive, for the check of the maximum, which must not
    wait for an average to catch up.
    """

    def __init__(self, hold_s: float, average: int = 1) -> None:
        """Keep a temperature hold_s seconds (of sample time) after its drive turned null; average the last samples."""
        self._hold = hold_s
        self._average = max(1, average)
        self._last: dict[str, tuple[float, float, datetime]] = {}
        # The last reading of each drive returned by per_drive, not averaged.
        self.raw: dict[str, float] = {}
        self._recent: dict[str, list[float]] = {}
        self._seen: str | None = None

    def per_drive(self, latest: dict[str, Any] | None, hwmon_count: int) -> dict[str, float]:
        """Return {log name: temperature} of the drives that count now (awake, or held)."""
        if not latest:
            self.raw = {}
            return {}
        when = datetime.fromisoformat(latest["time"])
        # A sample counts once in the average, however often it is read.
        new = latest["time"] != self._seen
        self._seen = latest["time"]
        temps: dict[str, float] = {}
        self.raw = {}
        for name, temp in _drive_temps(latest, hwmon_count):
            if temp is not None:
                recent = self._recent.setdefault(name, [])
                if new or not recent:
                    recent.append(temp)
                    del recent[: -self._average]
                value = sum(recent) / len(recent)
                self._last[name] = (value, temp, when)
                temps[name] = value
                self.raw[name] = temp
            elif name in self._last:
                held, held_raw, seen = self._last[name]
                if (when - seen).total_seconds() <= self._hold:
                    temps[name] = held
                    self.raw[name] = held_raw
                else:
                    del self._last[name]
                    self._recent.pop(name, None)
        return temps

    def hottest(self, latest: dict[str, Any] | None, hwmon_count: int) -> float | None:
        """Return the highest drive temperature, counting the ones held; None when there is none."""
        temps = self.per_drive(latest, hwmon_count)
        return max(temps.values()) if temps else None


# ---------------------------
#   _sensor_speed
# ---------------------------
def _sensor_speed(latest: dict[str, Any] | None, hwmons: tuple[Hwmon, ...]) -> float:
    """Return the speed that the extra hwmon sensors ask for, from 0 to 1.

    The hwmon inputs are the last values of a sample, in the order of the
    settings. Like the curve of hddfancontrol, a sensor asks for nothing up
    to its minimum, for full speed from its maximum, and in proportion
    between. A sensor without a range asks for nothing: its range is one
    that hddfancontrol picks for itself.
    """
    if not latest or not hwmons:
        return 0.0
    values = list(latest["temps"].values())[-len(hwmons) :]
    speed = 0.0
    for hwmon, temp in zip(hwmons, values, strict=False):
        if temp is None or hwmon.temp_min is None or hwmon.temp_max is None:
            continue
        speed = max(speed, min(1.0, max(0.0, (temp - hwmon.temp_min) / (hwmon.temp_max - hwmon.temp_min))))
    return speed


# ---------------------------
#   _is_stale
# ---------------------------
def _is_stale(age: float | None, stale_after: float) -> bool:
    """Whether a sample of this age (None: no sample) is too old, or too far in the future, to trust.

    A sample from more than FUTURE_TOLERANCE_S ahead means a clock that
    jumped: its age says nothing, and it would count as fresh for good.
    """
    return age is None or age > stale_after or age < -FUTURE_TOLERANCE_S


# ---------------------------
#   drive_ceiling
# ---------------------------
def drive_ceiling(kernel: str) -> float:
    """The temperature at which a drive's fans go to full speed whatever the settings say: NVMe or not.

    The settings may set a maximum too high for the drive (a typo, a
    misread datasheet); this ceiling holds in every mode, manual control
    included.
    """
    return NVME_CEILING if kernel.startswith("nvme") else DRIVE_CEILING


# ---------------------------
#   _age_seconds
# ---------------------------
def _age_seconds(latest: dict[str, Any] | None, now: datetime) -> float | None:
    """How old a sample is, or None without a sample."""
    if not latest:
        return None
    return (now - datetime.fromisoformat(latest["time"])).total_seconds()


# ---------------------------
#   PwmWriter
# ---------------------------
class PwmWriter:
    """Write PWM files and give each one back its mode at the end.

    The state an output had before it was first taken (its mode and value)
    is also kept in state_file, with the boot it belongs to. A run that
    was killed (SIGKILL, out of memory) never gave the outputs back: the
    next run of the same boot finds them in manual mode at its own last
    value, and would save that as the chip's state, leaving the fans fixed
    for good. It takes the saved state instead. After a reboot the chip
    starts afresh, so the state of another boot is ignored. An entry is
    removed once its output is given back.
    """

    def __init__(self, sys_root: Path = Path("/sys"), state_file: Path | None = None, boot_id_file: Path = BOOT_ID) -> None:
        """Write under this tree (the real /sys, or a copy in the tests); keep the chip's state in state_file."""
        self._sys = sys_root
        self._saved: dict[str, tuple[str | None, str | None]] = {}
        self.values: dict[str, int] = {}
        self._state_file = state_file
        try:
            self._boot = boot_id_file.read_text(encoding="utf-8").strip()
        except OSError:
            self._boot = ""
        self._original = self._load_state()

    def _load_state(self) -> dict[str, tuple[str | None, str | None]]:
        """The states saved by an earlier run of this boot; empty when there are none or the file is broken."""
        if self._state_file is None or not self._boot:
            return {}
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if data.get("boot") != self._boot:
                return {}
            outputs = {
                str(path): (None if enable is None else str(enable), None if value is None else str(value))
                for path, (enable, value) in data["outputs"].items()
            }
        except FileNotFoundError:
            return {}
        except Exception as err:  # broken or nested too deep: the outputs are taken as they are now
            _LOGGER.error("Cannot read %s, ignoring it: %r", self._state_file, err)
            return {}
        if outputs:
            _LOGGER.warning("The last run did not give the fans back: using the state it saved for %s", ", ".join(outputs))
        return outputs

    def _store_state(self) -> None:
        """Write the saved states, for a run after an unclean exit. Never raises: the fans come first."""
        if self._state_file is None or not self._boot:
            return
        document = {"boot": self._boot, "outputs": {path: list(state) for path, state in self._original.items()}}
        try:
            write_atomic(self._state_file, json.dumps(document))
        except OSError as err:
            _LOGGER.error("Cannot save the state of the fans in %s: %s", self._state_file, err)

    def _file(self, path: str) -> Path:
        """Map a real /sys path into the tree."""
        return self._sys / path.removeprefix("/sys/")

    def _read(self, path: str) -> str | None:
        """Read a sysfs file, or None."""
        try:
            return self._file(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def _take(self, path: str, always: bool) -> None:
        """Put the output in manual mode the first time, and only then save its state.

        Saved only once the mode is written: after a failed write, the next
        one tries the mode again instead of writing a value into a file that
        may not be a fan at all. An output whose driver has no pwmN_enable
        file is saved as it is. always: write the mode even when it already
        reads as manual.
        """
        if path in self._saved:
            return
        if not is_pwm_path(path):
            raise ValueError(f"{path} is not a PWM output")
        enable_file = self._file(f"{path}_enable")
        enable = self._read(f"{path}_enable")
        original = self._original.get(path)
        if original is None:
            original = (enable, self._read(path))
            self._original[path] = original
            self._store_state()
        if enable is None and not os.path.lexists(enable_file):
            self._saved[path] = original
            return
        if always or enable != str(ENABLE_MANUAL):
            enable_file.write_text(f"{ENABLE_MANUAL}\n", encoding="utf-8")
        self._saved[path] = original

    def write(self, path: str, value: int) -> None:
        """Take the output in manual mode, the first time, and set its value."""
        value = max(0, min(PWM_MAX, int(value)))
        self._take(path, always=True)
        self._file(path).write_text(f"{value}\n", encoding="utf-8")
        self.values[path] = value

    def take(self, path: str) -> None:
        """Put an output in manual mode without changing its value, saving its state the first time.

        Some drivers (nct6775) refuse a PWM value while the chip itself drives
        the output; hddfancontrol pwm-test writes values without switching
        the mode first, so the PWM test takes the output with this.
        """
        self._take(path, always=False)

    def forget(self) -> None:
        """Drop what was saved without giving it back: the outputs stay as they are now."""
        self._saved.clear()
        self.values.clear()

    def restore(self) -> None:
        """Give every output back the mode it had; one that was in manual mode already gets full speed.

        An output found in manual mode may hold a value that a killed run
        left: its old value is not trusted, and 255 is the safe one. An
        output without an enable file gets its value back.
        """
        for path, (enable, value) in self._saved.items():
            try:
                if enable is not None and enable != str(ENABLE_MANUAL):
                    self._file(f"{path}_enable").write_text(f"{enable}\n", encoding="utf-8")
                elif enable is None and value is not None:
                    self._file(path).write_text(f"{value}\n", encoding="utf-8")
                else:
                    self._file(path).write_text(f"{PWM_MAX}\n", encoding="utf-8")
            except OSError as err:
                _LOGGER.error("Cannot restore %s: %s", path, err)
                continue
            self._original.pop(path, None)
        if self._saved:
            self._store_state()
        self._saved.clear()
        self.values.clear()


# ---------------------------
#   FanController
# ---------------------------
@dataclass
class _ControlState:
    """What the curve or auto mode did last."""

    hottest: float | None = None
    speed: float | None = None
    failsafe: bool = False
    speeds: dict[str, float] = field(default_factory=dict)


class FanController:
    """Run the curve or auto mode, or manual control, with the safety rules."""

    def __init__(self, writer: PwmWriter, latest: LatestTemps, log: Log, drive_names: DriveNames | None = None) -> None:
        """Write with writer, read temperatures with latest, log with log.

        drive_names() maps every /dev path of a drive (its by-id names) to its
        kernel name (sda), the name in the temperature log.
        """
        self.writer = writer
        self._latest = latest
        self._log = log
        self._drive_names = drive_names or dict
        self._names: dict[str, str] = {}
        self._names_at = -NAMES_REFRESH_S
        self._task: asyncio.Task[None] | None = None
        self.mode: str | None = None
        self.safety = False
        # Why manual control went to full speed: SAFETY_HOT or SAFETY_STALE.
        self.safety_reason: str | None = None
        # The last unexpected error of the control loop, while it lasts.
        self.error: str | None = None
        self.auto = _ControlState()
        self._settings = Settings()
        # The API sets manual values from a worker thread while the safety
        # loop runs in the event loop: checking safety and writing must be one
        # step, or a value could land just after the fans went to full speed.
        self._manual_lock = threading.Lock()
        self._temps = DriveTemps(0)
        # Set when hddfancontrol ended: samples up to this time are not trusted.
        self._lost: str | None = None

    @property
    def active(self) -> bool:
        """Whether the controller drives the fans."""
        return self._task is not None and not self._task.done()

    def start_control(self, settings: Settings) -> None:
        """Drive the configured fans in the mode of the settings (curve or auto)."""
        self._settings = settings
        self.mode = settings.control_mode
        self.error = None
        self._lost = None
        self.auto = _ControlState()
        self._task = asyncio.create_task(self._run_control(settings), name="fan-control")

    def start_manual(self, settings: Settings) -> None:
        """Let the user set the fans, and watch for hot drives."""
        self._settings = settings
        self.mode = "manual"
        self.safety = False
        self.safety_reason = None
        self.error = None
        self._lost = None
        self._task = asyncio.create_task(self._run_manual(settings), name="fan-manual")

    def measuring_lost(self) -> None:
        """hddfancontrol ended: the temperatures logged so far are old news, fans at full speed until new ones come."""
        latest = self._latest()
        self._lost = latest["time"] if latest else ""

    def set_manual(self, path: str, value: int) -> None:
        """Set one PWM output in manual control."""
        with self._manual_lock:
            # Checked under the lock: stop() gives the fans back under it too,
            # so no value lands after they were given back.
            if self.mode != "manual":
                raise NotManual("Not in manual control")
            if self.safety:
                # Hot drives: the fans stay at full speed.
                return
            self.writer.write(path, value)

    async def stop(self, final: bool = False) -> None:
        """Stop and give the fans back.

        Only at the final stop (the web UI exits) does restore_fan_settings
        off leave the fans at full speed instead. A stop to restart (apply,
        a mode change) always gives them back: forgetting what was saved
        there would make the next run save "manual, 255" as the chip's own
        state, and the fans would never go back to the chip.
        """
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_wait(task)
        with self._manual_lock:
            controlled = self.mode in ("curve", "auto")
            self.mode = None
            if final and controlled and not self._settings.restore_fan_settings:
                self._full_speed([p.path for p in self._settings.pwms])
                self.writer.forget()
            else:
                try:
                    self.writer.restore()
                except OSError as err:
                    self._log(f"Cannot restore the fans: {err}")
        self.safety = False
        self.safety_reason = None
        self.error = None
        # The failsafe and speeds of a loop that ended must not show as current.
        self.auto = _ControlState()

    def _write(self, pwm: Pwm, speed: float) -> None:
        """Set one configured fan to a speed."""
        running = self.writer.values.get(pwm.path, 0) > 0
        try:
            self.writer.write(pwm.path, speed_to_pwm(speed, pwm, running))
        except OSError as err:
            self._log(f"Cannot write {pwm.path}: {err}")

    def _full_speed(self, paths: list[str]) -> None:
        """Put these outputs at full speed, whatever fails. Used when the control loop fails."""
        for path in dict.fromkeys(paths):
            try:
                self.writer.write(path, PWM_MAX)
            except Exception as err:  # the other fans must still be written
                self._log(f"Cannot write {path}: {err}")

    def _failed(self, err: Exception, paths: list[str]) -> None:
        """An unexpected error in a control loop: log it once and run every fan at full speed."""
        text = f"{type(err).__name__}: {err}"
        if text != self.error:
            self._log(f"ERROR: fan control failed, every fan goes to full speed: {text}")
            _LOGGER.exception("Fan control failed")
        self.error = text
        self._full_speed(paths)

    async def _refresh_names(self) -> None:
        """Read again the kernel name of every /dev path of a drive, every NAMES_REFRESH_S, in a thread (it reads /dev)."""
        if time.monotonic() - self._names_at >= NAMES_REFRESH_S:
            self._names = await asyncio.to_thread(self._drive_names)
            self._names_at = time.monotonic()

    def _kernel_names(self) -> dict[str, str]:
        """The kernel name of every /dev path of a drive, as last read by _refresh_names."""
        return self._names

    def _followed(self, settings: Settings) -> tuple[dict[str, set[str] | None], set[str] | None]:
        """For each fan, the kernel names of the drives it follows (None: every drive), and those some group follows.

        A fan without its own drives, or whose drives are all gone from the
        host, follows every drive: better too much air than too little. The
        second value is the drives some group follows, or None when a fan
        follows every drive; a drive outside every group (left unticked, or
        given as an interface name such as "ata") is followed by every fan.
        """
        names = self._kernel_names()
        result: dict[str, set[str] | None] = {}
        for pwm in settings.pwms:
            own = {names[d] for d in pwm.drives if d in names}
            result[pwm.path] = own or None
        covered: set[str] | None = set()
        for group in result.values():
            covered = None if group is None or covered is None else covered | group
        return result, covered

    def _entries(
        self, temps: dict[str, float], follows: set[str] | None, covered: set[str] | None, settings: Settings
    ) -> list[tuple[str, float, float, tuple[float, float, float]]]:
        """(log name, averaged temperature, last reading, (min, target, max)) of the drives a fan follows.

        The log names a drive "sda MODEL"; its limits are those of the
        settings drive with that kernel name, or the common ones (a drive
        given as an interface name such as "ata").
        """
        names = self._names
        by_kernel = {names[d]: d for d in settings.drives if d in names}
        entries = []
        for name, temp in temps.items():
            kernel = name.split()[0]
            if follows is None or kernel in follows or (covered is not None and kernel not in covered):
                raw = self._temps.raw.get(name, temp)
                low, target, high = settings.limits_of(by_kernel.get(kernel, ""))
                # Full speed at the ceiling too, if the settings ask for more (the curve keeps its own range).
                raw = raw if raw < drive_ceiling(kernel) else max(raw, high)
                entries.append((name, temp, raw, (low, target, high)))
        return entries

    async def _run_control(self, settings: Settings) -> None:
        """Every interval, read the temperatures and set the speed of each fan."""
        interval = float(settings.interval_s)
        stale_after = max(3 * interval, 120.0)
        min_speed = settings.min_fan_speed_prct / 100
        controllers = {p.path: PiController(settings.target_temp, settings.drive_temp_max, min_speed) for p in settings.pwms}
        started = datetime.now(UTC)
        self._temps = DriveTemps(NULL_HOLD_INTERVALS * interval, settings.average)
        # Curve mode: the temperature each drive last asked for (it must cool CURVE_COOLDOWN to ask for less).
        asked: dict[str, float] = {}
        if settings.control_mode == "auto":
            self._log(f"Auto mode: target {settings.target_temp:g} °C, full speed at {settings.drive_temp_max:g} °C")
        else:
            self._log(
                f"Curve mode: {settings.drive_temp_min:g} to {settings.drive_temp_max:g} °C, cooling down by {CURVE_COOLDOWN:g} °C"
            )
        if settings.drive_limits:
            self._log("Own limits: " + ", ".join(f"{d.rsplit('/', 1)[-1]} {v}" for d, v in settings.drive_limits.items()))
        unranged = [h.path for h in settings.hwmons if h.temp_min is None or h.temp_max is None]
        if unranged:
            self._log(f"WARNING: fan control uses only the sensors with a range; without one: {', '.join(unranged)}")
        while True:
            try:
                await self._refresh_names()
                self._control_step(settings, controllers, asked, interval, stale_after, started)
            except Exception as err:  # the loop must outlive any error: full speed
                self.auto.speed = 1.0
                self._failed(err, [p.path for p in settings.pwms])
            await asyncio.sleep(interval)

    def _control_step(
        self,
        settings: Settings,
        controllers: dict[str, PiController],
        asked: dict[str, float],
        interval: float,
        stale_after: float,
        started: datetime,
    ) -> None:
        """Read the temperatures once and set the speed of each fan."""
        now = datetime.now(UTC)
        latest = self._latest()
        age = _age_seconds(latest, now)
        waiting = (now - started).total_seconds() < stale_after
        if self._lost is not None and latest and latest["time"] > self._lost:
            self._lost = None
        lost = self._lost is not None
        if lost or _is_stale(age, stale_after):
            failsafe = lost or not waiting
            if failsafe and not self.auto.failsafe:
                why = "hddfancontrol stopped" if lost else "no recent drive temperature"
                self._log(f"WARNING: {why}, fans at full speed")
            self.auto.failsafe = failsafe
            self.auto.hottest = None
            self.auto.speed = 1.0
            self.auto.speeds = {p.path: 1.0 for p in settings.pwms}
            for pwm in settings.pwms:
                self._write(pwm, 1.0)
        else:
            if self.auto.failsafe:
                self._log("Drive temperatures are back, fan control resumes")
            self.auto.failsafe = False
            temps = self._temps.per_drive(latest, len(settings.hwmons))
            sensor = _sensor_speed(latest, settings.hwmons)
            min_speed = settings.min_fan_speed_prct / 100
            follows, covered = self._followed(settings)
            # Curve mode: what each drive asks for, with the cool-down margin.
            for name, temp in temps.items():
                if temp > asked.get(name, -1000.0) or temp <= asked[name] - CURVE_COOLDOWN:
                    asked[name] = temp
            speeds: dict[str, float] = {}
            for pwm in settings.pwms:
                entries = self._entries(temps, follows[pwm.path], covered, settings)
                if settings.control_mode == "auto":
                    error = max((temp - limits[1] for _, temp, _, limits in entries), default=None)
                    # The maximum is checked on the last reading: an average would only delay full speed.
                    at_max = any(raw >= limits[2] for _, _, raw, limits in entries)
                    speed = controllers[pwm.path].step_error(error, at_max, interval)
                else:
                    wanted = max(
                        (curve_speed(asked[name], low, high, min_speed) for name, _, _, (low, _, high) in entries),
                        default=min_speed,
                    )
                    if any(raw >= high for _, _, raw, (_, _, high) in entries):
                        wanted = 1.0
                    before = self.auto.speeds.get(pwm.path, 1.0)
                    speed = max(wanted, before - MAX_DECREASE_PER_STEP)
                # The drives and the extra sensors each ask for a speed: the higher one wins.
                speed = max(speed, sensor)
                speeds[pwm.path] = speed
                self._write(pwm, speed)
            self.auto.hottest = max(temps.values()) if temps else None
            self.auto.speeds = speeds
            self.auto.speed = max(speeds.values()) if speeds else None
        if self.error is not None:
            self._log("Fan control works again")
            self.error = None

    async def _run_manual(self, settings: Settings) -> None:
        """Watch the drives while the user sets the fans.

        A drive at its maximum, or, when drives are set, temperatures that
        stop coming in, put every fan at full speed until manual control
        ends. Without drives there is nothing to watch, so manual control
        stays usable to find out which fan is which before the drives are
        chosen.
        """
        stale_after = max(3 * float(settings.interval_s), 120.0)
        started = datetime.now(UTC)
        self._temps = DriveTemps(NULL_HOLD_INTERVALS * float(settings.interval_s))
        while True:
            try:
                await self._refresh_names()
                self._manual_step(settings, stale_after, started)
            except Exception as err:  # the loop must outlive any error: full speed
                with self._manual_lock:
                    self.safety = True
                    self.safety_reason = SAFETY_ERROR
                    self._failed(err, list(self.writer.values) + [p.path for p in settings.pwms])
            # The temperatures change only once per interval of hddfancontrol.
            await asyncio.sleep(settings.interval_s)

    def drive_danger(
        self, settings: Settings, temps_state: DriveTemps, started: datetime, stale_after: float
    ) -> tuple[str | None, float | None]:
        """Check the drives once: (SAFETY_HOT, hottest), (SAFETY_STALE, None) or (None, None).

        SAFETY_HOT when a drive is at its maximum; SAFETY_STALE when drives
        are set and no sample is newer than stale_after (counted from
        started). Blocking (reads the temperature log). Used by manual control and by
        the watch of the PWM test; temps_state keeps a sleeping drive's last
        temperature between calls.
        """
        now = datetime.now(UTC)
        latest = self._latest()
        temps_state.per_drive(latest, len(settings.hwmons))
        names = self._kernel_names()
        by_kernel = {names[d]: d for d in settings.drives if d in names}
        # The maximum is checked on the last reading of each drive: an average would only delay full speed.
        hot = [
            temp
            for name, temp in temps_state.raw.items()
            if temp >= min(settings.limits_of(by_kernel.get(name.split()[0], ""))[2], drive_ceiling(name.split()[0]))
        ]
        if hot:
            return SAFETY_HOT, max(hot)
        age = _age_seconds(latest, now)
        waiting = (now - started).total_seconds() < stale_after
        if settings.drives and not waiting and _is_stale(age, stale_after):
            return SAFETY_STALE, None
        return None, None

    async def watch_drives(
        self, settings: Settings, temps_state: DriveTemps, started: datetime, stale_after: float
    ) -> tuple[str | None, float | None]:
        """drive_danger, with the drive names refreshed first; the blocking part runs in a thread."""
        await self._refresh_names()
        return await asyncio.to_thread(self.drive_danger, settings, temps_state, started, stale_after)

    def _manual_step(self, settings: Settings, stale_after: float, started: datetime) -> None:
        """Check the drives once; a drive at its maximum, old temperatures or a stopped hddfancontrol put every fan at full speed."""
        lost = self._lost
        if lost is not None:
            latest = self._latest()
            if latest and latest["time"] > lost:
                self._lost = lost = None
        reason: str | None
        hottest: float | None
        if lost is not None:
            reason, hottest = SAFETY_STALE, None
        else:
            reason, hottest = self.drive_danger(settings, self._temps, started, stale_after)
        if reason is not None and not self.safety:
            with self._manual_lock:
                self.safety = True
                self.safety_reason = reason
                for path in list(self.writer.values) + [p.path for p in settings.pwms]:
                    with contextlib.suppress(OSError):
                        self.writer.write(path, PWM_MAX)
            if reason == SAFETY_HOT:
                self._log(f"WARNING: a drive is at {hottest:g} °C, every fan goes to full speed")
            else:
                self._log("WARNING: no recent drive temperature, every fan goes to full speed")

    def status(self) -> dict[str, Any]:
        """Return the state as JSON-ready data."""
        return {
            "mode": self.mode,
            "safety": self.safety,
            "safety_reason": self.safety_reason,
            "error": self.error,
            "values": dict(self.writer.values),
            "auto": {
                "hottest": self.auto.hottest,
                "speed": self.auto.speed,
                "failsafe": self.auto.failsafe,
                "speeds": dict(self.auto.speeds),
            },
        }
