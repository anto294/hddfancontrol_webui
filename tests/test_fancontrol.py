"""Auto mode, manual control and their safety rules."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui import fancontrol
from hddfancontrol_webui.fancontrol import DriveTemps, FanController, PiController, PwmWriter, speed_to_pwm
from hddfancontrol_webui.settings import Pwm, from_dict

PWM1 = "/sys/class/hwmon/hwmon2/pwm1"
PWM2 = "/sys/class/hwmon/hwmon2/pwm2"


def read(fake_sys: Path, name: str) -> str:
    """Read a file of the fake chip."""
    return (fake_sys / "class" / "hwmon" / "hwmon2" / name).read_text().strip()


def test_speed_to_pwm() -> None:
    """0 stops, above 0 goes from the stop value to 255, a stopped fan gets its start value."""
    pwm = Pwm(PWM1, 200, 75)
    assert speed_to_pwm(0, pwm, running=True) == 0
    assert speed_to_pwm(1, pwm, running=True) == 255
    assert speed_to_pwm(2, pwm, running=True) == 255
    assert speed_to_pwm(0.1, pwm, running=True) == 93
    assert speed_to_pwm(0.1, pwm, running=False) == 200


def test_pi_controller_follows_the_target() -> None:
    """Hotter than the target speeds up, cooler slows down gently, and the limits hold."""
    pi = PiController(target=40, maximum=50, min_speed=0.2)
    assert pi.speed == 1.0
    # Cool drives: the speed goes down at most 10 % per step, to the minimum.
    speeds = [pi.step(35, 20) for _ in range(12)]
    assert speeds[0] == pytest.approx(0.9)
    assert all(a >= b for a, b in zip(speeds, speeds[1:], strict=False))
    assert speeds[-1] == pytest.approx(0.2)
    # Hotter than the target: the speed rises at once and keeps rising.
    hot = [pi.step(43, 20) for _ in range(5)]
    assert hot[0] > 0.2 and hot[-1] > hot[0]
    # At the maximum: full speed whatever the history.
    assert pi.step(50, 20) == 1.0
    # Every drive asleep: minimum speed.
    assert pi.step(None, 20) == 0.2


def test_pi_controller_settles() -> None:
    """A simple thermal model settles near the target without oscillating."""
    pi = PiController(target=40, maximum=50, min_speed=0.0)
    temp = 45.0
    for _ in range(400):
        speed = pi.step(temp, 20)
        # Heat in is constant; heat out grows with the fan speed.
        temp += 0.02 * (48 - temp) - 0.05 * speed * (temp - 25) + 0.02 * (25 - temp) * 0.1
    assert abs(temp - 40) < 1.0


def test_writer_takes_and_gives_back(fake_sys: Path) -> None:
    """The first write sets manual mode; restore gives back the mode, or the value."""
    writer = PwmWriter(fake_sys)
    writer.write(PWM1, 300)
    writer.write(PWM1, 90)
    writer.write(PWM2, -5)
    assert read(fake_sys, "pwm1_enable") == "1"
    assert read(fake_sys, "pwm1") == "90"
    assert read(fake_sys, "pwm2") == "0"
    assert writer.values == {PWM1: 90, PWM2: 0}
    writer.restore()
    # pwm1 was automatic (5): the chip takes over again.
    assert read(fake_sys, "pwm1_enable") == "5"
    # pwm2 was manual already: its value comes back.
    assert read(fake_sys, "pwm2") == "255"
    assert writer.values == {}


def test_writer_without_enable_file(fake_sys: Path) -> None:
    """A PWM output without an enable file is left at full speed."""
    chip = fake_sys / "class" / "hwmon" / "hwmon2"
    (chip / "pwm3").write_text("100\n")
    writer = PwmWriter(fake_sys)
    (chip / "pwm3_enable").unlink(missing_ok=True)
    writer._saved["/sys/class/hwmon/hwmon2/pwm3"] = (None, None)
    writer.restore()
    assert (chip / "pwm3").read_text().strip() == "255"


def sample(temps: dict[str, float | None], age: float = 0) -> dict[str, Any]:
    """A temperature sample age seconds old."""
    return {"time": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(), "temps": temps}


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make asyncio.sleep in the controller nearly instant."""
    real = asyncio.sleep

    async def quick(_: float) -> None:
        await real(0.01)

    monkeypatch.setattr(fancontrol.asyncio, "sleep", quick)


SETTINGS = from_dict(
    {
        "drives": ["/dev/sda", "/dev/sdb"],
        "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
        "hwmons": [{"path": "/sys/class/hwmon/hwmon1/temp1_input"}],
        "control_mode": "auto",
        "target_temp": 40,
        "drive_temp_max": 50,
        "interval_s": 1,
    }
)


async def test_auto_mode_drives_the_fans(fake_sys: Path, fast: None) -> None:
    """The hottest drive, not the hwmon input, sets the speed."""
    latest = sample({"sda": 35.0, "sdb": None, "coretemp": 90.0})
    lines: list[str] = []
    fans = FanController(PwmWriter(fake_sys), lambda: latest, lines.append)
    fans.start_control(SETTINGS)
    assert fans.active
    await asyncio.sleep(0.1)
    assert fans.auto.hottest == 35.0
    assert fans.auto.speed is not None and fans.auto.speed < 1.0
    assert read(fake_sys, "pwm1_enable") == "1"
    await fans.stop()
    assert read(fake_sys, "pwm1_enable") == "5"
    assert not fans.active and fans.mode is None


def test_hottest_counts_every_drive_of_an_interface() -> None:
    """An interface name gives several drives: all of them count, the hwmon inputs do not."""
    latest = sample({"sda A": 35.0, "sdb B": 52.0, "sdc C": None, "coretemp.0/1": 90.0})
    assert DriveTemps(0).hottest(latest, 1) == 52.0
    assert DriveTemps(0).hottest(sample({"sda A": 35.0, "sdb B": 41.0}), 0) == 41.0
    assert DriveTemps(0).hottest(sample({"sda A": None, "coretemp.0/1": 90.0}), 1) is None
    # Fewer values than hwmon inputs: no drive, never an hwmon value taken for one.
    assert DriveTemps(0).hottest(sample({"coretemp.0/1": 90.0}), 2) is None


def test_null_drive_keeps_its_last_temperature_for_a_while() -> None:
    """A failed probe (null) of a hot drive keeps it counting; a drive asleep for long drops out."""
    temps = DriveTemps(hold_s=100)
    start = datetime.now(UTC)

    def at(seconds: float, value: float | None) -> dict[str, Any]:
        return {
            "time": (start + timedelta(seconds=seconds)).isoformat(),
            "temps": {"sda A": value, "sdb B": 30.0, "cpu": 70.0},
        }

    assert temps.hottest(at(0, 48.0), 1) == 48.0
    assert temps.hottest(at(60, None), 1) == 48.0
    assert temps.hottest(at(100, None), 1) == 48.0
    assert temps.hottest(at(160, None), 1) == 30.0
    assert temps.hottest(at(200, None), 1) == 30.0
    assert temps.hottest(None, 1) is None


def test_sensor_speed() -> None:
    """A sensor with a range asks for a speed in proportion; one without a range asks for nothing."""
    hwmons = from_dict(
        {
            "hwmons": [
                {"path": "/sys/class/hwmon/hwmon1/temp1_input", "temp_min": 40, "temp_max": 60},
                {"path": "/sys/class/hwmon/hwmon1/temp2_input"},
                {"path": "/sys/class/hwmon/hwmon1/temp3_input", "temp_min": 30, "temp_max": 40},
            ]
        }
    ).hwmons
    latest = sample({"sda A": 35.0, "a/1": 50.0, "b/1": 99.0, "c/1": 20.0})
    assert fancontrol._sensor_speed(latest, hwmons) == 0.5
    assert fancontrol._sensor_speed(sample({"sda A": 35.0, "a/1": 70.0, "b/1": 0.0, "c/1": 0.0}), hwmons) == 1.0
    assert fancontrol._sensor_speed(latest, ()) == 0.0
    assert fancontrol._sensor_speed(None, hwmons) == 0.0


async def test_auto_mode_follows_a_hot_sensor(fake_sys: Path, fast: None) -> None:
    """A sensor with a range past its maximum puts the fans at full speed with cool drives."""
    settings = from_dict(
        {
            "drives": ["/dev/sda"],
            "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
            "hwmons": [
                {"path": "/sys/class/hwmon/hwmon1/temp1_input", "temp_min": 40, "temp_max": 60},
                {"path": "/sys/class/hwmon/hwmon1/temp2_input"},
            ],
            "control_mode": "auto",
            "interval_s": 1,
        }
    )
    lines: list[str] = []
    latest = sample({"sda A": 30.0, "x/1": 65.0, "y/1": 90.0})
    fans = FanController(PwmWriter(fake_sys), lambda: latest, lines.append)
    fans.start_control(settings)
    await asyncio.sleep(0.1)
    assert fans.auto.hottest == 30.0 and fans.auto.speed == 1.0
    assert read(fake_sys, "pwm1") == "255"
    assert any("temp2_input" in line and "range" in line for line in lines)
    await fans.stop()


async def test_auto_mode_failsafe(fake_sys: Path, fast: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without recent temperatures, full speed; when they come back, control resumes."""
    current: dict[str, Any] = {"value": sample({"sda": 30.0}, age=3600)}
    lines: list[str] = []
    monkeypatch.setattr(fancontrol, "datetime", _Later)
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lines.append)
    fans.start_control(SETTINGS)
    await asyncio.sleep(0.1)
    assert fans.auto.failsafe and fans.auto.speed == 1.0
    assert read(fake_sys, "pwm1") == "255"
    current["value"] = {"time": _Later.now(UTC).isoformat(), "temps": {"sda": 30.0, "coretemp.0/1": 60.0}}
    await asyncio.sleep(0.1)
    assert not fans.auto.failsafe
    assert any("full speed" in line for line in lines) and any("resumes" in line for line in lines)
    await fans.stop()


class _Later(datetime):
    """A clock that runs one hour ahead after the controller starts, to end the grace period."""

    calls = 0

    @classmethod
    def now(cls, tz: Any = None) -> _Later:  # type: ignore[override]  # matches datetime.now
        """Return the real time, plus one hour from the second call on."""
        cls.calls += 1
        real = datetime.now(tz)
        shifted = real + timedelta(hours=1) if cls.calls > 1 else real
        return cls.fromtimestamp(shifted.timestamp(), tz)


async def test_manual_control(fake_sys: Path, fast: None) -> None:
    """Manual values are written; a hot drive forces full speed and blocks the sliders."""
    current: dict[str, Any] = {"value": sample({"sda": 35.0, "coretemp.0/1": 60.0})}
    lines: list[str] = []
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lines.append)
    with pytest.raises(RuntimeError):
        fans.set_manual(PWM2, 10)
    fans.start_manual(SETTINGS)
    fans.set_manual(PWM2, 60)
    assert read(fake_sys, "pwm2") == "60"
    current["value"] = sample({"sda": 51.0, "coretemp.0/1": 60.0})
    await asyncio.sleep(0.1)
    assert fans.safety
    assert read(fake_sys, "pwm2") == "255" and read(fake_sys, "pwm1") == "255"
    fans.set_manual(PWM2, 10)
    assert read(fake_sys, "pwm2") == "255"
    assert fans.status()["safety"] is True
    await fans.stop()
    assert read(fake_sys, "pwm1_enable") == "5"


async def test_manual_control_without_recent_temperatures(fake_sys: Path, fast: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """With drives set, temperatures that stop coming in put the fans at full speed; without drives, nothing does."""
    monkeypatch.setattr(fancontrol, "datetime", _Later)
    _Later.calls = 0
    old = sample({"sda A": 30.0, "coretemp.0/1": 50.0}, age=3600)
    lines: list[str] = []
    fans = FanController(PwmWriter(fake_sys), lambda: old, lines.append)
    fans.start_manual(SETTINGS)
    fans.set_manual(PWM2, 60)
    await asyncio.sleep(0.1)
    assert fans.safety and fans.status()["safety_reason"] == "stale"
    assert read(fake_sys, "pwm2") == "255" and read(fake_sys, "pwm1") == "255"
    assert any("no recent drive temperature" in line for line in lines)
    await fans.stop()
    assert fans.status()["safety_reason"] is None

    _Later.calls = 0
    no_drives = from_dict({"pwms": [{"path": PWM1, "start": 200, "stop": 75}], "interval_s": 1})
    fans = FanController(PwmWriter(fake_sys), lambda: None, lines.append)
    fans.start_manual(no_drives)
    fans.set_manual(PWM1, 90)
    await asyncio.sleep(0.1)
    assert not fans.safety and read(fake_sys, "pwm1") == "90"
    await fans.stop()


def test_manual_step_after_hddfancontrol_ended(fake_sys: Path) -> None:
    """A stopped hddfancontrol counts as stale in manual control, unless a newer sample came meanwhile."""
    current: dict[str, Any] = {"value": sample({"sda": 30.0, "coretemp.0/1": 50.0})}
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lambda text: None)
    started = datetime.now(UTC)
    fans.measuring_lost()
    current["value"] = sample({"sda": 30.0, "coretemp.0/1": 50.0}, age=-60)  # newer: measuring again
    fans._manual_step(SETTINGS, 3600, started)
    assert not fans.safety and fans._lost is None
    fans.measuring_lost()
    fans._manual_step(SETTINGS, 3600, started)
    assert fans.safety and fans.safety_reason == "stale"
    assert read(fake_sys, "pwm1") == "255"


def test_drive_danger_checks_the_last_reading(fake_sys: Path) -> None:
    """A drive is at its maximum on its last reading, never on an average that lags behind."""
    current: dict[str, Any] = {}
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lambda text: None)
    temps = DriveTemps(60, average=3)
    started = datetime.now(UTC)
    for age, temp in ((20, 40.0), (10, 40.0), (0, 52.0)):  # averages 40, 40, then 44: under 50
        current["value"] = sample({"sda": temp, "coretemp.0/1": 50.0}, age=age)
        reason, hottest = fans.drive_danger(SETTINGS, temps, started, 3600)
    assert (reason, hottest) == ("hot", 52.0)


async def test_auto_mode_survives_an_error(fake_sys: Path, fast: None) -> None:
    """An unexpected error puts the fans at full speed, the loop goes on, and control resumes."""
    current: dict[str, Any] = {"value": "broken"}
    lines: list[str] = []
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lines.append)
    fans.start_control(SETTINGS)
    await asyncio.sleep(0.1)
    assert fans.active and fans.error is not None and fans.status()["error"] == fans.error
    assert read(fake_sys, "pwm1") == "255"
    assert sum("fan control failed" in line for line in lines) == 1
    current["value"] = sample({"sda": 30.0, "coretemp.0/1": 50.0})
    await asyncio.sleep(0.1)
    assert fans.error is None and any("works again" in line for line in lines)
    await fans.stop()
    assert read(fake_sys, "pwm1_enable") == "5"


async def test_manual_control_survives_an_error(fake_sys: Path, fast: None) -> None:
    """An unexpected error in manual control puts every fan touched at full speed and blocks the sliders."""
    current: dict[str, Any] = {"value": sample({"sda": 35.0, "coretemp.0/1": 60.0})}
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lambda _: None)
    fans.start_manual(SETTINGS)
    fans.set_manual(PWM2, 60)
    current["value"] = "broken"
    await asyncio.sleep(0.1)
    assert fans.active and fans.safety and fans.safety_reason == "error" and fans.error
    assert read(fake_sys, "pwm1") == "255" and read(fake_sys, "pwm2") == "255"
    fans.set_manual(PWM2, 10)
    assert read(fake_sys, "pwm2") == "255"
    await fans.stop()
    assert fans.error is None


async def test_manual_values_never_land_after_stop(fake_sys: Path, fast: None) -> None:
    """Values set from worker threads while manual control ends are refused, never written after the restore."""
    fans = FanController(PwmWriter(fake_sys), lambda: sample({"sda": 30.0, "coretemp.0/1": 50.0}), lambda _: None)
    fans.start_manual(SETTINGS)
    fans.set_manual(PWM1, 90)

    def slider() -> bool:
        """Move the slider until manual control has ended; True when a value was refused."""
        end = time.monotonic() + 5
        value = 0
        while time.monotonic() < end:
            try:
                fans.set_manual(PWM1, value % 100)
            except RuntimeError:
                return True
            value += 1
        return False

    threads = [asyncio.to_thread(slider) for _ in range(4)]
    await asyncio.sleep(0.01)
    *refused, _ = await asyncio.gather(*threads, fans.stop())
    assert all(refused)
    assert read(fake_sys, "pwm1_enable") == "5"
    assert fans.writer.values == {}


A = "/dev/disk/by-id/ata-A"
B = "/dev/disk/by-id/ata-B"
NAMES = {A: "sda", B: "sdb"}


async def steps(count: int) -> None:
    """Wait for about count control steps (asyncio.sleep is short with the fast fixture)."""
    for _ in range(count):
        await asyncio.sleep(1)


def test_curve_speed() -> None:
    """The curve: minimum up to the minimum temperature, full speed from the maximum, a line between."""
    assert fancontrol.curve_speed(25, 30, 50, 0.2) == 0.2
    assert fancontrol.curve_speed(40, 30, 50, 0.2) == pytest.approx(0.6)
    assert fancontrol.curve_speed(55, 30, 50, 0.2) == 1.0


async def test_curve_mode_cools_down_before_slowing(fake_sys: Path, fast: None) -> None:
    """A drive going back and forth by one degree keeps its speed; cooler by the margin, the fans slow down gently."""
    current: dict[str, Any] = {"temp": 40.0}
    settings = from_dict(
        {
            "drives": [A],
            "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
            "drive_temp_min": 30,
            "drive_temp_max": 50,
            "min_fan_speed_prct": 20,
            "interval_s": 1,
        }
    )

    def latest() -> dict[str, Any]:
        return {"time": datetime.now(UTC).isoformat(), "temps": {"sda A": current["temp"]}}

    fans = FanController(PwmWriter(fake_sys), latest, lambda _: None, lambda: NAMES)
    fans.start_control(settings)
    await steps(30)
    assert fans.mode == "curve" and fans.auto.speed == pytest.approx(0.6)
    current["temp"] = 39.0
    await steps(20)
    assert fans.auto.speed == pytest.approx(0.6)  # one degree back: no change
    current["temp"] = 41.0
    await steps(20)
    assert fans.auto.speed == pytest.approx(0.64)  # warmer: at once
    current["temp"] = 30.0
    seen = []
    for _ in range(30):
        await steps(1)
        seen.append(fans.auto.speed)
    # Slowing down takes steps of at most 10 %, down to the minimum.
    assert any(speed is not None and 0.2 < speed < 0.6 for speed in seen)
    assert fans.auto.speed == pytest.approx(0.2)
    current["temp"] = 50.0
    await steps(5)
    assert fans.auto.speed == 1.0
    await fans.stop()


async def test_fan_groups_and_drive_limits(fake_sys: Path, fast: None) -> None:
    """Each fan follows its own drives with their own limits; a fan of drives that are gone follows every drive."""
    settings = from_dict(
        {
            "drives": [A, B],
            "pwms": [
                {"path": PWM1, "start": 200, "stop": 75, "drives": [A]},
                {"path": PWM2, "start": 200, "stop": 75, "drives": [B]},
            ],
            "drive_limits": {A: {"min": 30, "max": 40}},
            "drive_temp_min": 30,
            "drive_temp_max": 50,
            "min_fan_speed_prct": 0,
            "interval_s": 1,
        }
    )
    sample = sample_now = {"temps": {"sda A": 40.0, "sdb B": 35.0}}
    names = dict(NAMES)

    def latest() -> dict[str, Any]:
        return {"time": datetime.now(UTC).isoformat(), **sample_now}

    fans = FanController(PwmWriter(fake_sys), latest, lambda _: None, lambda: names)
    fans.start_control(settings)
    await steps(40)
    # sda is at its own maximum (40): full speed; sdb at 35 of 30-50: a quarter.
    assert fans.auto.speeds[PWM1] == 1.0
    assert fans.auto.speeds[PWM2] == pytest.approx(0.25)
    # Drive B is gone from the host: its fan follows every drive.
    names.pop(B)
    fans._names_at = -1e9
    await steps(20)
    assert fans.auto.speeds[PWM2] == 1.0
    assert sample is sample_now
    await fans.stop()


async def test_auto_mode_follows_each_drive_target(fake_sys: Path, fast: None) -> None:
    """In the auto mode a drive over its own target counts more than a hotter drive under its own."""
    settings = from_dict(
        {
            "drives": [A, B],
            "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
            "control_mode": "auto",
            "drive_limits": {B: {"target": 30, "max": 40}},
            "target_temp": 40,
            "drive_temp_max": 50,
            "interval_s": 1,
        }
    )
    sample = {"temps": {"sda A": 39.0, "sdb B": 40.0}}
    fans = FanController(
        PwmWriter(fake_sys), lambda: {"time": datetime.now(UTC).isoformat(), **sample}, lambda _: None, lambda: NAMES
    )
    fans.start_control(settings)
    await steps(10)
    assert fans.auto.speed == 1.0  # sdb is at its own maximum
    # Both under their targets (sda by 5, sdb by 1): the fans slow down.
    sample["temps"] = {"sda A": 35.0, "sdb B": 29.0}
    await steps(30)
    slow = fans.auto.speed
    assert slow is not None and slow < 1.0
    # sdb 6 over its own target, sda cooler than ever: the fans speed up for sdb.
    sample["temps"] = {"sda A": 34.0, "sdb B": 36.0}
    await steps(10)
    assert fans.auto.speed is not None and fans.auto.speed > slow
    await fans.stop()


async def test_restore_off_leaves_full_speed(fake_sys: Path, fast: None) -> None:
    """With restore_fan_settings off, the fans stay at full speed when the web UI exits, not at a restart."""
    settings = from_dict({**SETTINGS.to_dict(), "restore_fan_settings": False})
    fans = FanController(PwmWriter(fake_sys), lambda: sample({"sda": 30.0, "coretemp.0/1": 50.0}), lambda _: None)
    fans.start_control(settings)
    await steps(10)
    # A stop to restart (apply) gives the fans back, so the chip's state is never lost.
    await fans.stop()
    assert read(fake_sys, "pwm1_enable") == "5"
    fans.start_control(settings)
    await steps(10)
    await fans.stop(final=True)
    assert read(fake_sys, "pwm1") == "255" and read(fake_sys, "pwm1_enable") == "1"


def test_drive_temps_average() -> None:
    """The temperature of a drive is the average of its last samples, each counted once."""
    temps = DriveTemps(hold_s=100, average=3)
    start = datetime.now(UTC)
    for seconds, value in ((0, 40.0), (0, 40.0), (20, 43.0), (40, 46.0), (60, 49.0)):
        result = temps.per_drive({"time": (start + timedelta(seconds=seconds)).isoformat(), "temps": {"sda": value}}, 0)
    assert result == {"sda": pytest.approx(46.0)}


async def test_full_speed_does_not_wait_for_the_average(fake_sys: Path, fast: None) -> None:
    """With AVERAGE, a drive that reaches its maximum puts its fans at full speed at once, in both modes."""
    for mode in ("auto", "curve"):
        current = {"temp": 35.0, "time": datetime.now(UTC)}
        settings = from_dict({**SETTINGS.to_dict(), "drives": [A], "control_mode": mode, "average": 10, "hwmons": []})

        def latest(current: dict[str, Any] = current) -> dict[str, Any]:
            return {"time": current["time"].isoformat(), "temps": {"sda A": current["temp"]}}

        fans = FanController(PwmWriter(fake_sys), latest, lambda _: None, lambda: NAMES)
        fans.start_control(settings)
        await steps(15)
        assert fans.auto.speed is not None and fans.auto.speed < 1.0
        current.update(temp=50.0, time=datetime.now(UTC))
        await steps(3)
        assert fans.auto.speed == 1.0, mode
        assert fans.auto.hottest is not None and fans.auto.hottest < 50.0  # the average is still catching up
        await fans.stop()


def test_drive_temps_raw_reading() -> None:
    """raw holds the last reading of each drive, held like the average while the drive is null."""
    temps = DriveTemps(hold_s=100, average=3)
    start = datetime.now(UTC)
    for seconds, value in ((0, 40.0), (20, 43.0), (40, 49.0)):
        temps.per_drive({"time": (start + timedelta(seconds=seconds)).isoformat(), "temps": {"sda": value}}, 0)
    assert temps.raw == {"sda": 49.0}
    temps.per_drive({"time": (start + timedelta(seconds=60)).isoformat(), "temps": {"sda": None}}, 0)
    assert temps.raw == {"sda": 49.0}
    temps.per_drive(None, 0)
    assert temps.raw == {}


async def test_a_drive_outside_every_group_is_followed_by_every_fan(fake_sys: Path, fast: None) -> None:
    """When every fan has its group, a drive in none of them still drives every fan."""
    c = "/dev/disk/by-id/ata-C"
    settings = from_dict(
        {
            "drives": [A, B, c],
            "pwms": [
                {"path": PWM1, "start": 200, "stop": 75, "drives": [A]},
                {"path": PWM2, "start": 200, "stop": 75, "drives": [B]},
            ],
            "drive_temp_min": 30,
            "drive_temp_max": 50,
            "min_fan_speed_prct": 0,
            "interval_s": 1,
        }
    )
    temps = {"sda A": 30.0, "sdb B": 30.0, "sdc C": 50.0}
    fans = FanController(
        PwmWriter(fake_sys),
        lambda: {"time": datetime.now(UTC).isoformat(), "temps": temps},
        lambda _: None,
        lambda: {**NAMES, c: "sdc"},
    )
    fans.start_control(settings)
    await steps(10)
    assert fans.auto.speeds == {PWM1: 1.0, PWM2: 1.0}
    # Cool again: each fan follows its own drive only (at its minimum).
    temps["sdc C"] = 30.0
    await steps(40)
    assert fans.auto.speeds == {PWM1: 0.0, PWM2: 0.0}
    await fans.stop()


async def test_stop_clears_the_failsafe(fake_sys: Path, fast: None) -> None:
    """After hddfancontrol stopped (failsafe), stopping control clears it, so manual mode shows no safety alert."""
    fixed = sample({"sda": 30.0})  # no newer sample comes after the loss
    fans = FanController(PwmWriter(fake_sys), lambda: fixed, lambda _: None)
    fans.start_control(SETTINGS)
    fans.measuring_lost()
    await steps(3)
    assert fans.status()["auto"]["failsafe"] is True
    await fans.stop()
    assert fans.status()["auto"] == {"hottest": None, "speed": None, "failsafe": False, "speeds": {}}
    fans.start_manual(SETTINGS)
    assert fans.status()["auto"]["failsafe"] is False
    await fans.stop()


async def test_drive_names_are_read_in_a_thread(fake_sys: Path, fast: None) -> None:
    """Hardware.kernel_names reads /dev: the control loop calls it off the event loop."""
    import threading

    threads: list[threading.Thread] = []

    def names() -> dict[str, str]:
        threads.append(threading.current_thread())
        return NAMES

    fans = FanController(PwmWriter(fake_sys), lambda: sample({"sda A": 30.0}), lambda _: None, names)
    fans.start_control(from_dict({**SETTINGS.to_dict(), "drives": [A], "hwmons": []}))
    await steps(3)
    await fans.stop()
    assert threads and threading.main_thread() not in threads


def test_writer_saves_an_output_only_once_it_is_taken(fake_sys: Path) -> None:
    """A failed write of the mode leaves nothing saved, so the next write fails too instead of writing the value."""
    chip = fake_sys / "class" / "hwmon" / "hwmon2"
    (chip / "pwm4").write_text("120\n")
    (chip / "pwm4_enable").mkdir()  # cannot be written
    path = "/sys/class/hwmon/hwmon2/pwm4"
    writer = PwmWriter(fake_sys)
    for _ in range(2):
        with pytest.raises(OSError):
            writer.write(path, 200)
        with pytest.raises(OSError):
            writer.take(path)
    assert (chip / "pwm4").read_text() == "120\n" and writer.values == {}
    writer.restore()  # nothing to give back
    assert (chip / "pwm4").read_text() == "120\n"


def test_writer_writes_only_pwm_outputs(fake_sys: Path) -> None:
    """Whatever the caller passes, only /sys/class/hwmon/hwmonN/pwmN files are written."""
    (fake_sys / "block" / "sda" / "device").mkdir(parents=True)
    delete = fake_sys / "block" / "sda" / "device" / "delete"
    delete.write_text("")
    writer = PwmWriter(fake_sys)
    for _ in range(2):
        with pytest.raises(ValueError):
            writer.write("/sys/block/sda/device/delete", 255)
    with pytest.raises(ValueError):
        writer.take("/sys/block/sda/device/delete")
    assert delete.read_text() == ""


def test_writer_without_an_enable_file_still_writes(fake_sys: Path) -> None:
    """An output whose driver has no pwmN_enable is written at once, and gets its value back from restore."""
    chip = fake_sys / "class" / "hwmon" / "hwmon2"
    (chip / "pwm5").write_text("100\n")
    writer = PwmWriter(fake_sys)
    writer.write("/sys/class/hwmon/hwmon2/pwm5", 80)
    assert (chip / "pwm5").read_text() == "80\n" and not (chip / "pwm5_enable").exists()
    writer.restore()
    assert (chip / "pwm5").read_text() == "100\n"


def boot(tmp_path: Path, text: str) -> Path:
    """A boot_id file with this boot id."""
    path = tmp_path / "boot_id"
    path.write_text(text + "\n")
    return path


def test_writer_keeps_the_chip_state_across_an_unclean_exit(fake_sys: Path, tmp_path: Path) -> None:
    """Killed without restore, the next run of the same boot gives the fans the chip's state, not its own leftovers."""
    state = tmp_path / "config" / "pwm-state.json"
    first = PwmWriter(fake_sys, state, boot(tmp_path, "boot-1"))
    first.write(PWM1, 40)  # the chip had mode 5; SIGKILL now, no restore
    assert read(fake_sys, "pwm1_enable") == "1" and state.exists()
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    second = PwmWriter(fake_sys, state, boot(tmp_path, "boot-1"))
    second.write(PWM1, 60)
    second.restore()
    assert read(fake_sys, "pwm1_enable") == "5"  # not "1" with 40, which would leave the fan fixed for good
    assert json.loads(state.read_text())["outputs"] == {}  # given back: forgotten


def test_writer_ignores_the_state_of_another_boot(fake_sys: Path, tmp_path: Path) -> None:
    """After a reboot the chip starts afresh: what an earlier boot saved no longer holds."""
    state = tmp_path / "pwm-state.json"
    PwmWriter(fake_sys, state, boot(tmp_path, "boot-1")).write(PWM1, 40)
    (fake_sys / "class/hwmon/hwmon2/pwm1_enable").write_text("2\n")  # what the BIOS set at this boot
    writer = PwmWriter(fake_sys, state, boot(tmp_path, "boot-2"))
    writer.write(PWM1, 60)
    writer.restore()
    assert read(fake_sys, "pwm1_enable") == "2"


def test_writer_gives_full_speed_to_an_output_found_in_manual_mode(fake_sys: Path, tmp_path: Path) -> None:
    """An output already in manual mode at the first take may hold a value left by a killed run: restore puts 255."""
    (fake_sys / "class/hwmon/hwmon2/pwm2").write_text("30\n")  # pwm2_enable is 1 in the fake tree
    writer = PwmWriter(fake_sys, tmp_path / "pwm-state.json", boot(tmp_path, "boot-1"))
    writer.write(PWM2, 90)
    writer.restore()
    assert read(fake_sys, "pwm2") == "255"


def test_writer_survives_a_broken_state_file(fake_sys: Path, tmp_path: Path) -> None:
    """An unreadable state file is ignored: the outputs are taken as they are now."""
    state = tmp_path / "pwm-state.json"
    state.write_text("[" * 100000)
    writer = PwmWriter(fake_sys, state, boot(tmp_path, "boot-1"))
    writer.write(PWM1, 60)
    writer.restore()
    assert read(fake_sys, "pwm1_enable") == "5"


def test_drive_ceiling() -> None:
    """65 °C for SATA/SAS drives, 80 °C for NVMe drives, whatever the settings say."""
    assert fancontrol.drive_ceiling("sda") == 65 and fancontrol.drive_ceiling("nvme0n1") == 80


async def test_the_ceiling_wins_over_the_settings_in_every_mode(fake_sys: Path, fast: None) -> None:
    """A SATA drive at 65 °C puts its fans at full speed even with a maximum of 70 in the settings, manual control too."""
    settings = from_dict(
        {
            "drives": ["/dev/sda"],
            "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
            "drive_temp_min": 30,
            "target_temp": 60,
            "drive_temp_max": 70,
            "min_fan_speed_prct": 0,
            "interval_s": 1,
        }
    )
    hot = sample({"sda": 65.0})
    for mode in ("curve", "auto", "manual"):
        fans = FanController(PwmWriter(fake_sys), lambda: hot, lambda text: None, lambda: {"/dev/sda": "sda"})
        if mode == "manual":
            fans.start_manual(settings)
        else:
            fans.start_control(replace(settings, control_mode=mode))
        await steps(3)
        assert fans.writer.values.get(PWM1) == 255, mode
        await fans.stop()
    warm = sample({"sda": 64.0})
    fans = FanController(PwmWriter(fake_sys), lambda: warm, lambda text: None, lambda: {"/dev/sda": "sda"})
    fans.start_control(replace(settings, control_mode="curve"))
    await steps(3)
    assert fans.writer.values.get(PWM1, 0) < 255  # below the ceiling the curve rules
    await fans.stop()


async def test_a_sample_from_the_future_is_stale(fake_sys: Path, fast: None) -> None:
    """A sample timed more than a minute ahead (a clock that jumped back) never counts as fresh: full speed."""
    future = sample({"sda": 30.0}, age=-3600)
    fans = FanController(PwmWriter(fake_sys), lambda: future, lambda text: None)
    fans.start_control(SETTINGS)
    await asyncio.sleep(0.1)
    assert fans.auto.speed == 1.0 and fans.auto.hottest is None and read(fake_sys, "pwm1") == "255"
    await fans.stop()
    assert not fancontrol._is_stale(-30.0, 120.0) and fancontrol._is_stale(-61.0, 120.0)
    assert fancontrol._is_stale(None, 120.0) and fancontrol._is_stale(121.0, 120.0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_writer_with_an_enable_file_it_cannot_read(fake_sys: Path) -> None:
    """An enable file that exists but cannot be read is still switched to manual; giving it back means full speed.

    Its old mode is unknown, so restore cannot put it back: writing only the
    old value would leave the fan in manual mode at a speed the chip had
    picked for that moment, for good.
    """
    enable = fake_sys / "class/hwmon/hwmon2/pwm1_enable"
    enable.chmod(0o200)  # writable, not readable
    writer = PwmWriter(fake_sys)
    writer.write(PWM1, 40)
    writer.restore()
    enable.chmod(0o644)
    assert read(fake_sys, "pwm1_enable") == "1" and read(fake_sys, "pwm1") == "255"
