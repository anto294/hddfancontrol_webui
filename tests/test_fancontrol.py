"""Auto mode, manual control and their safety rules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui import fancontrol
from hddfancontrol_webui.fancontrol import FanController, PiController, PwmWriter, speed_to_pwm
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
        "drives": ["/dev/a", "/dev/b"],
        "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
        "hwmons": [{"path": "/sys/x"}],
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
    fans.start_auto(SETTINGS)
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
    assert fancontrol._hottest(latest, 1) == 52.0
    assert fancontrol._hottest(sample({"sda A": 35.0, "sdb B": 41.0}), 0) == 41.0
    assert fancontrol._hottest(sample({"sda A": None, "coretemp.0/1": 90.0}), 1) is None


def test_sensor_speed() -> None:
    """A sensor with a range asks for a speed in proportion; one without a range asks for nothing."""
    hwmons = from_dict(
        {
            "hwmons": [
                {"path": "/sys/a", "temp_min": 40, "temp_max": 60},
                {"path": "/sys/b"},
                {"path": "/sys/c", "temp_min": 30, "temp_max": 40},
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
            "drives": ["/dev/a"],
            "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
            "hwmons": [{"path": "/sys/x", "temp_min": 40, "temp_max": 60}, {"path": "/sys/y"}],
            "control_mode": "auto",
            "interval_s": 1,
        }
    )
    lines: list[str] = []
    latest = sample({"sda A": 30.0, "x/1": 65.0, "y/1": 90.0})
    fans = FanController(PwmWriter(fake_sys), lambda: latest, lines.append)
    fans.start_auto(settings)
    await asyncio.sleep(0.1)
    assert fans.auto.hottest == 30.0 and fans.auto.speed == 1.0
    assert read(fake_sys, "pwm1") == "255"
    assert any("/sys/y" in line and "range" in line for line in lines)
    await fans.stop()


async def test_auto_mode_failsafe(fake_sys: Path, fast: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without recent temperatures, full speed; when they come back, control resumes."""
    current: dict[str, Any] = {"value": sample({"sda": 30.0}, age=3600)}
    lines: list[str] = []
    monkeypatch.setattr(fancontrol, "datetime", _Later)
    fans = FanController(PwmWriter(fake_sys), lambda: current["value"], lines.append)
    fans.start_auto(SETTINGS)
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
