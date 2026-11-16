"""Fan control done by the web UI itself: the auto mode and manual control.

In the curve mode hddfancontrol drives the fans. In the auto mode and in
manual control it only measures: it runs with ``--fan-cmd /bin/true``, keeps
probing the drives without waking them, and writes the temperature log. This
module reads that log and writes the PWM files.

Auto mode is a PI controller: it keeps the hottest drive near a target
temperature with as little fan speed as that needs, and adapts by itself to
load, season and case. It is feedback control, not machine learning.

Safety, in both modes:

- A drive at or above the maximum temperature puts every fan at full speed.
- In auto mode, a temperature log that stops updating also puts every fan
  at full speed, since the controller no longer knows the temperatures.
- When control ends, each PWM output gets back the pwmN_enable value it had,
  so the chip or hddfancontrol takes over again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .settings import Hwmon, Pwm, Settings

_LOGGER = logging.getLogger(__name__)

PWM_MAX = 255
ENABLE_MANUAL = 1

# Gains of the PI controller, per °C of error. The proportional part adds 8 %
# of speed per °C above the target; the integral part adds about 9 % per
# minute for each °C that stays above it. Drive temperatures move over
# minutes, so these stay calm and never oscillate in practice.
KP = 0.08
KI = 0.0015
# Slowing down is limited to this much speed per step, so a short dip does
# not stop the fans; speeding up is never limited.
MAX_DECREASE_PER_STEP = 0.10

# Why manual control put every fan at full speed.
SAFETY_HOT = "hot"
SAFETY_STALE = "stale"

LatestTemps = Callable[[], dict[str, Any] | None]
Log = Callable[[str], None]


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
    """Keep the hottest drive at a target temperature."""

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
            self.integral = self.min_speed
            self.speed = self.min_speed
            return self.speed
        if hottest >= self.maximum:
            self.integral = 1.0
            self.speed = 1.0
            return self.speed
        error = hottest - self.target
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
#   _hottest
# ---------------------------
def _hottest(latest: dict[str, Any] | None, hwmon_count: int) -> float | None:
    """Return the highest drive temperature of a sample.

    hddfancontrol logs every drive first, then every hwmon input, and an
    hwmon input is always there. So the drives are every value but the last
    hwmon_count, however many drives an interface name such as 'ata' gave.
    """
    if not latest:
        return None
    values = list(latest["temps"].values())
    values = values[: len(values) - hwmon_count]
    temps = [t for t in values if t is not None]
    return max(temps) if temps else None


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
    """Write PWM files and give each one back its mode at the end."""

    def __init__(self, sys_root: Path = Path("/sys")) -> None:
        """Write under this tree: the real /sys, or a copy in the tests."""
        self._sys = sys_root
        self._saved: dict[str, tuple[str | None, str | None]] = {}
        self.values: dict[str, int] = {}

    def _file(self, path: str) -> Path:
        """Map a real /sys path into the tree."""
        return self._sys / path.removeprefix("/sys/")

    def _read(self, path: str) -> str | None:
        """Read a sysfs file, or None."""
        try:
            return self._file(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def write(self, path: str, value: int) -> None:
        """Take the output in manual mode, the first time, and set its value."""
        value = max(0, min(PWM_MAX, int(value)))
        if path not in self._saved:
            self._saved[path] = (self._read(f"{path}_enable"), self._read(path))
            self._file(f"{path}_enable").write_text(f"{ENABLE_MANUAL}\n", encoding="utf-8")
        self._file(path).write_text(f"{value}\n", encoding="utf-8")
        self.values[path] = value

    def restore(self) -> None:
        """Give every output back the mode, and in manual mode the value, it had."""
        for path, (enable, value) in self._saved.items():
            try:
                if enable is not None and enable != str(ENABLE_MANUAL):
                    self._file(f"{path}_enable").write_text(f"{enable}\n", encoding="utf-8")
                elif value is not None:
                    self._file(path).write_text(f"{value}\n", encoding="utf-8")
                else:
                    self._file(path).write_text(f"{PWM_MAX}\n", encoding="utf-8")
            except OSError as err:
                _LOGGER.error("Cannot restore %s: %s", path, err)
        self._saved.clear()
        self.values.clear()


# ---------------------------
#   FanController
# ---------------------------
@dataclass
class _AutoState:
    """What the auto mode did last."""

    hottest: float | None = None
    speed: float | None = None
    failsafe: bool = False


class FanController:
    """Run the auto mode or manual control, with the safety rules."""

    def __init__(self, writer: PwmWriter, latest: LatestTemps, log: Log) -> None:
        """Write with writer, read temperatures with latest, log with log."""
        self.writer = writer
        self._latest = latest
        self._log = log
        self._task: asyncio.Task[None] | None = None
        self.mode: str | None = None
        self.safety = False
        # Why manual control went to full speed: SAFETY_HOT or SAFETY_STALE.
        self.safety_reason: str | None = None
        self.auto = _AutoState()
        self._settings = Settings()
        # The API sets manual values from a worker thread while the safety
        # loop runs in the event loop: checking safety and writing must be one
        # step, or a value could land just after the fans went to full speed.
        self._manual_lock = threading.Lock()

    @property
    def active(self) -> bool:
        """Whether the controller drives the fans."""
        return self._task is not None and not self._task.done()

    def start_auto(self, settings: Settings) -> None:
        """Drive the configured fans to keep the drives at the target."""
        self._settings = settings
        self.mode = "auto"
        self.auto = _AutoState()
        self._task = asyncio.create_task(self._run_auto(settings), name="fan-auto")

    def start_manual(self, settings: Settings) -> None:
        """Let the user set the fans, and watch for hot drives."""
        self._settings = settings
        self.mode = "manual"
        self.safety = False
        self.safety_reason = None
        self._task = asyncio.create_task(self._run_manual(settings), name="fan-manual")

    def set_manual(self, path: str, value: int) -> None:
        """Set one PWM output in manual control."""
        if self.mode != "manual":
            raise RuntimeError("Not in manual control")
        with self._manual_lock:
            if self.safety:
                # Hot drives: the fans stay at full speed.
                return
            self.writer.write(path, value)

    async def stop(self) -> None:
        """Stop and give the fans back."""
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_wait(task)
        try:
            self.writer.restore()
        except OSError as err:
            self._log(f"Cannot restore the fans: {err}")
        self.mode = None
        self.safety = False
        self.safety_reason = None

    def _write_all(self, pwms: tuple[Pwm, ...], speed: float) -> None:
        """Set every configured fan to a speed."""
        for pwm in pwms:
            running = self.writer.values.get(pwm.path, 0) > 0
            try:
                self.writer.write(pwm.path, speed_to_pwm(speed, pwm, running))
            except OSError as err:
                self._log(f"Cannot write {pwm.path}: {err}")

    async def _run_auto(self, settings: Settings) -> None:
        """Every interval, read the temperatures and set the speed."""
        controller = PiController(settings.target_temp, settings.drive_temp_max, settings.min_fan_speed_prct / 100)
        interval = float(settings.interval_s)
        stale_after = max(3 * interval, 120.0)
        hwmons = len(settings.hwmons)
        started = datetime.now(UTC)
        self._log(f"Auto mode: target {settings.target_temp:g} °C, full speed at {settings.drive_temp_max:g} °C")
        unranged = [h.path for h in settings.hwmons if h.temp_min is None or h.temp_max is None]
        if unranged:
            self._log(f"WARNING: auto mode uses only the sensors with a range; without one: {', '.join(unranged)}")
        while True:
            now = datetime.now(UTC)
            latest = self._latest()
            age = _age_seconds(latest, now)
            waiting = (now - started).total_seconds() < stale_after
            if age is None or age > stale_after:
                if not waiting and not self.auto.failsafe:
                    self._log("WARNING: no recent drive temperature, fans at full speed")
                self.auto.failsafe = not waiting
                speed = 1.0
                hottest = None
            else:
                if self.auto.failsafe:
                    self._log("Drive temperatures are back, auto mode resumes")
                self.auto.failsafe = False
                hottest = _hottest(latest, hwmons)
                # The drives and the extra sensors each ask for a speed: the higher one wins.
                speed = max(controller.step(hottest, interval), _sensor_speed(latest, settings.hwmons))
            self.auto.hottest = hottest
            self.auto.speed = speed
            self._write_all(settings.pwms, speed)
            await asyncio.sleep(interval)

    async def _run_manual(self, settings: Settings) -> None:
        """Watch the drives while the user sets the fans.

        A hot drive, or, when drives are set, temperatures that stop coming
        in, put every fan at full speed until manual control ends. Without
        drives there is nothing to watch, so manual control stays usable to
        find out which fan is which before the drives are chosen.
        """
        hwmons = len(settings.hwmons)
        stale_after = max(3 * float(settings.interval_s), 120.0)
        started = datetime.now(UTC)
        while True:
            now = datetime.now(UTC)
            latest = self._latest()
            hottest = _hottest(latest, hwmons)
            age = _age_seconds(latest, now)
            waiting = (now - started).total_seconds() < stale_after
            reason = None
            if hottest is not None and hottest >= settings.drive_temp_max:
                reason = SAFETY_HOT
            elif settings.drives and not waiting and (age is None or age > stale_after):
                reason = SAFETY_STALE
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
            # The temperatures change only once per interval of hddfancontrol.
            await asyncio.sleep(settings.interval_s)

    def status(self) -> dict[str, Any]:
        """Return the state as JSON-ready data."""
        return {
            "mode": self.mode,
            "safety": self.safety,
            "safety_reason": self.safety_reason,
            "values": dict(self.writer.values),
            "auto": {"hottest": self.auto.hottest, "speed": self.auto.speed, "failsafe": self.auto.failsafe},
        }
