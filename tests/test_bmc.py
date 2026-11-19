"""The BMC of a server through a fake ipmitool (experimental): sensors, Dell fan control and its safety rules."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui import bmc as bmc_module
from hddfancontrol_webui.bmc import (
    BMC_PWM,
    DELL,
    HPE,
    STALE_S,
    Bmc,
    Reading,
    hot_sensor,
    parse_sensors,
    parse_vendor,
    percent_of,
)
from hddfancontrol_webui.fancontrol import PwmWriter
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.settings import Pwm, Settings, from_dict, hddfancontrol_args

DELL_INFO = """Device ID                 : 32
Firmware Revision         : 2.86
Manufacturer ID           : 674
Manufacturer Name         : DELL Inc
Product ID                : 256 (0x0100)
Product Name              : iDRAC8
"""
HPE_INFO = """Device ID                 : 19
Manufacturer ID           : 11
Product Name              : iLO 4
"""
DELL_SENSORS = """Fan1             | 3600.000   | RPM        | ok    | na        | 360.000   | 600.000   | na        | na        | na
Fan2             | 3480.000   | RPM        | ok    | na        | 360.000   | 600.000   | na        | na        | na
Inlet Temp       | 23.000     | degrees C  | ok    | na        | -7.000    | 3.000     | 42.000    | 47.000    | na
Exhaust Temp     | 35.000     | degrees C  | ok    | na        | 0.000     | 5.000     | 70.000    | 75.000    | na
Temp             | 48.000     | degrees C  | ok    | na        | 3.000     | 8.000     | na        | 89.000    | na
Intrusion        | 0x0        | discrete   | 0x0080| na        | na        | na        | na        | na        | na
Current 1        | 0.400      | Amps       | ok    | na        | na        | na        | na        | na        | na
"""
HPE_SENSORS = """01-Inlet Ambient | 22.000     | degrees C  | ok    | na        | na        | na        | na        | 42.000    | 46.000
02-CPU 1         | 40.000     | degrees C  | ok    | na        | na        | na        | na        | 70.000    | na
Fan 1            | 23.520     | percent    | ok    | na        | na        | na        | na        | na        | na
Fan 2            | na         | percent    | na    | na        | na        | na        | na        | na        | na
Power Supply 1   | 80         | Watts      | ok    | na        | na        | na        | na        | na        | na
"""


class FakeIpmi:
    """A fake ipmitool: records its arguments, answers from files of a folder."""

    def __init__(self, folder: Path, info: str, sensors: str) -> None:
        """Write the program and its answers in folder."""
        self.folder = folder
        self.calls = folder / "calls"
        (folder / "mcinfo").write_text(info)
        (folder / "sensor").write_text(sensors)
        self.program = folder / "ipmitool"
        self.program.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.calls}"\n'
            'case "$1" in\n'
            f'  mc) cat "{folder}/mcinfo" ;;\n'
            f'  sensor) if [ -f "{folder}/fail_sensor" ]; then echo boom >&2; exit 1; fi; cat "{folder}/sensor" ;;\n'
            f'  raw) if [ -f "{folder}/fail_raw" ]; then echo "Invalid command" >&2; exit 1; fi ;;\n'
            "esac\n"
        )
        self.program.chmod(self.program.stat().st_mode | stat.S_IXUSR)
        self.device = folder / "ipmi0"
        self.device.write_text("")

    def take_calls(self) -> list[str]:
        """The commands run since the last look."""
        if not self.calls.exists():
            return []
        lines = self.calls.read_text().splitlines()
        self.calls.unlink()
        return lines

    def fail(self, what: str, failing: bool = True) -> None:
        """Make sensor or raw fail (or work again)."""
        flag = self.folder / f"fail_{what}"
        if failing:
            flag.write_text("")
        else:
            flag.unlink(missing_ok=True)


def make(tmp_path: Path, info: str = DELL_INFO, sensors: str = DELL_SENSORS) -> tuple[Bmc, FakeIpmi, list[str]]:
    """A Bmc on a fake ipmitool, and the lines it logged."""
    fake = FakeIpmi(tmp_path, info, sensors)
    logged: list[str] = []
    return Bmc(str(fake.program), fake.device, logged.append), fake, logged


def test_parse_sensors() -> None:
    """Temperatures with their upper non-critical (else critical) limit, fan speeds and duties; the rest is skipped."""
    dell = parse_sensors(DELL_SENSORS)
    assert [(r.name, r.kind, r.value, r.limit) for r in dell] == [
        ("Fan1", "fan", 3600.0, None),
        ("Fan2", "fan", 3480.0, None),
        ("Inlet Temp", "temp", 23.0, 42.0),
        ("Exhaust Temp", "temp", 35.0, 70.0),
        ("Temp", "temp", 48.0, 89.0),
    ]
    hpe = parse_sensors(HPE_SENSORS)
    assert [(r.name, r.kind, r.value, r.limit) for r in hpe] == [
        ("01-Inlet Ambient", "temp", 22.0, 42.0),
        ("02-CPU 1", "temp", 40.0, 70.0),
        ("Fan 1", "fan_pct", 23.52, None),
        ("Fan 2", "fan_pct", None, None),
    ]
    assert parse_sensors("garbage\n|||\n") == []


def test_parse_vendor_and_percent() -> None:
    """Dell (674) and HPE (11, 47196) by their enterprise number; PWM values become duties."""
    assert parse_vendor(DELL_INFO) == (DELL, "iDRAC8")
    assert parse_vendor(HPE_INFO) == (HPE, "iLO 4")
    assert parse_vendor("Manufacturer ID : 47196\n")[0] == HPE
    assert parse_vendor("Manufacturer ID : 10876\n") == (None, "")
    assert parse_vendor("Manufacturer ID : unknown\n") == (None, "")
    assert (percent_of(0), percent_of(128), percent_of(255), percent_of(400)) == (0, 50, 100, 100)


def test_hot_sensor() -> None:
    """Within the margin of the limit, or not ok; a few degrees lower once given back; 80 °C without a limit."""
    inlet = Reading("Inlet Temp", "temp", 36.0, "ok", 42.0)
    assert hot_sensor([inlet], handing_back=False) is None
    assert hot_sensor([Reading("Inlet Temp", "temp", 37.0, "ok", 42.0)], handing_back=False) is not None
    # Given back: it stays so until 3 degrees lower.
    assert hot_sensor([inlet], handing_back=True) is not None
    assert hot_sensor([Reading("Inlet Temp", "temp", 33.0, "ok", 42.0)], handing_back=True) is None
    assert hot_sensor([Reading("CPU", "temp", 76.0, "ok", None)], handing_back=False) is not None
    assert hot_sensor([Reading("CPU", "temp", 70.0, "ok", None)], handing_back=False) is None
    assert hot_sensor([Reading("X", "temp", 30.0, "cr", 90.0)], handing_back=False) is not None
    assert hot_sensor([Reading("Fan1", "fan", 99999.0, "cr"), Reading("T", "temp", None, "ns")], handing_back=False) is None


async def test_no_bmc(tmp_path: Path) -> None:
    """Without /dev/ipmi0 or without ipmitool nothing runs, and nothing shows."""
    bmc, fake, _ = make(tmp_path)
    bmc._device = tmp_path / "none"
    await bmc.start()
    assert not bmc.present and fake.take_calls() == []
    assert bmc.hwmon() is None and bmc.status() is None
    missing = Bmc(str(tmp_path / "no-ipmitool"), fake.device)
    await missing.start()
    assert not missing.present
    await missing.close()


async def test_bmc_that_does_not_answer(tmp_path: Path) -> None:
    """A device whose BMC does not answer is reported and left alone."""
    bmc, fake, logged = make(tmp_path)
    (fake.folder / "mcinfo").unlink()
    fake.program.write_text("#!/bin/sh\nexit 1\n")
    await bmc.start()
    assert not bmc.present
    assert "does not answer" in logged[0]


async def test_dell_drives_its_fans(tmp_path: Path) -> None:
    """A Dell gets its fans back at start, then takes what the fan controller asks, and gives them back."""
    bmc, fake, logged = make(tmp_path)
    await bmc.start()
    try:
        assert bmc.present and bmc.can_drive and bmc.name == "iDRAC"
        assert fake.take_calls() == ["mc info", "raw 0x30 0x30 0x01 0x01", "sensor"]
        assert "can be driven" in logged[0]
        device = bmc.hwmon()
        assert device is not None and device.name == "iDRAC"
        assert [(p.path, p.value, p.enable, p.rpm) for p in device.pwms] == [(BMC_PWM, None, 2, 3540)]
        assert [t.label for t in device.temps] == ["Inlet Temp", "Exhaust Temp", "Temp"]

        bmc.write(128)
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x02 0xff 0x32"]
        await bmc._apply()
        assert fake.take_calls() == []  # nothing new to send
        bmc.write(255)
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x02 0xff 0x64"]
        status = bmc.status()
        assert status is not None and status["manual"] and status["percent"] == 100 and status["drives_fans"]
        device = bmc.hwmon()
        assert device is not None and (device.pwms[0].value, device.pwms[0].enable) == (255, 1)

        bmc.release()
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"]
        assert not bmc.state.manual
    finally:
        await bmc.close()


async def test_dell_hands_back_when_hot(tmp_path: Path) -> None:
    """A temperature near its limit gives the fans back to the iDRAC, but never full speed; they come back cooler."""
    hot = DELL_SENSORS.replace("Inlet Temp       | 23.000", "Inlet Temp       | 39.000")
    bmc, fake, logged = make(tmp_path, sensors=hot)
    await bmc.start()
    fake.take_calls()
    bmc.write(100)
    await bmc._apply()
    assert fake.take_calls() == []  # never taken: the iDRAC keeps them
    assert bmc.state.handback == "Inlet Temp 39 °C"
    assert "go back to the iDRAC" in logged[-1]
    bmc.write(255)
    await bmc._apply()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x02 0xff 0x64"]
    bmc.write(100)
    await bmc._apply()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"]
    # Cooler, but within the hysteresis: still the iDRAC's.
    (fake.folder / "sensor").write_text(DELL_SENSORS.replace("Inlet Temp       | 23.000", "Inlet Temp       | 35.000"))
    await bmc._poll()
    await bmc._apply()
    assert fake.take_calls() == ["sensor"]
    (fake.folder / "sensor").write_text(DELL_SENSORS)
    await bmc._poll()
    await bmc._apply()
    assert fake.take_calls() == ["sensor", "raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x02 0xff 0x27"]
    assert bmc.state.handback is None and "drives the fans again" in logged[-1]
    await bmc.close()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"]


async def test_dell_hands_back_when_the_sensors_stop(tmp_path: Path) -> None:
    """Sensors that cannot be read for STALE_S give the fans back; the failure is logged once."""
    bmc, fake, logged = make(tmp_path)
    now = [1000.0]
    bmc._clock = lambda: now[0]
    await bmc.start()
    bmc.write(100)
    await bmc._apply()
    assert bmc.state.manual
    fake.fail("sensor")
    await bmc._poll()
    await bmc._poll()
    assert sum("cannot read the sensors" in line for line in logged) == 1
    now[0] += STALE_S + 1
    fake.take_calls()
    await bmc._apply()
    assert bmc.state.handback == "stale" and fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"]
    status = bmc.status()
    assert status is not None and status["error"]
    fake.fail("sensor", failing=False)
    await bmc._poll()
    assert bmc.state.error is None and "read again" in logged[-1]
    await bmc._apply()
    assert bmc.state.manual and bmc.state.handback is None
    await bmc.close()


async def test_dell_commands_that_fail(tmp_path: Path) -> None:
    """A refused command is logged; the speed is sent again at the next round; giving back fails without raising."""
    bmc, fake, logged = make(tmp_path)
    await bmc.start()
    fake.fail("raw")
    bmc.write(100)
    await bmc._apply()
    assert "Invalid command" in logged[-1] and bmc.state.percent is None
    bmc.state.manual = True
    await bmc._auto()
    assert "cannot give the fans back" in logged[-1] and bmc.state.manual
    fake.fail("raw", failing=False)
    await bmc._apply()
    assert bmc.state.percent == 39
    await bmc.close()
    assert not bmc.state.manual


async def test_slow_ipmitool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ipmitool that does not answer is killed after the timeout."""
    bmc, fake, _ = make(tmp_path)
    fake.program.write_text("#!/bin/sh\nsleep 5\n")
    monkeypatch.setattr(bmc_module, "TIMEOUT_S", 0.2)
    with pytest.raises(bmc_module.BmcError, match="no answer"):
        await bmc._run("sensor")


async def test_hpe_is_read_only(tmp_path: Path) -> None:
    """An iLO shows its temperatures and fans; no command is ever sent to its fans."""
    bmc, fake, logged = make(tmp_path, HPE_INFO, HPE_SENSORS)
    await bmc.start()
    assert bmc.present and not bmc.can_drive and bmc.name == "iLO"
    assert "read only" in logged[0]
    await bmc.close()
    assert fake.take_calls() == ["mc info", "sensor"]
    device = bmc.hwmon()
    assert device is not None and device.pwms == [] and len(device.temps) == 2
    status = bmc.status()
    assert status is not None and not status["drives_fans"] and status["readings"][2]["kind"] == "fan_pct"


async def test_the_loop_sends_and_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop sends what is asked and reads the sensors again after POLL_S."""
    bmc, fake, _ = make(tmp_path)
    monkeypatch.setattr(bmc_module, "APPLY_S", 0.01)
    monkeypatch.setattr(bmc_module, "POLL_S", 0.0)
    await bmc.start()
    fake.take_calls()
    bmc.write(51)
    for _ in range(100):
        if bmc.state.percent == 20:
            break
        await asyncio.sleep(0.02)
    await bmc.close()
    calls = fake.take_calls()
    assert "raw 0x30 0x30 0x02 0xff 0x14" in calls and "sensor" in calls
    assert calls[-1] == "raw 0x30 0x30 0x01 0x01"


class FakeBmc:
    """What PwmWriter needs of a BMC."""

    def __init__(self) -> None:
        """Nothing asked yet."""
        self.calls: list[Any] = []

    def write(self, value: int) -> None:
        """Record a value."""
        self.calls.append(value)

    def release(self) -> None:
        """Record a release."""
        self.calls.append("release")


def test_pwm_writer_sends_the_bmc_output_to_the_bmc(fake_sys: Path) -> None:
    """BMC_PWM goes to the BMC, is given back to it, and cannot be tested; other outputs are files as before."""
    fake = FakeBmc()
    writer = PwmWriter(fake_sys, bmc=fake)
    writer.write(BMC_PWM, 300)
    writer.write("/sys/class/hwmon/hwmon2/pwm1", 90)
    assert fake.calls == [255] and writer.values[BMC_PWM] == 255
    with pytest.raises(ValueError, match="cannot be tested"):
        writer.take(BMC_PWM)
    writer.restore()
    assert fake.calls == [255, "release"]
    assert (fake_sys / "class/hwmon/hwmon2/pwm1_enable").read_text().strip() == "5"
    with pytest.raises(ValueError, match="no BMC"):
        PwmWriter(fake_sys).write(BMC_PWM, 10)


async def test_hardware_shows_the_bmc(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """The BMC is one more hwmon device; its output has no chip in /sys."""
    bmc, _, _ = make(tmp_path)
    await bmc.start()
    hardware = Hardware(fake_dev, fake_sys, bmc=bmc.hwmon)
    assert [d.hwmon for d in hardware.hwmons()] == ["hwmon2", "hwmon10", "ipmi"]
    assert BMC_PWM in hardware.pwm_paths()
    assert hardware.chip_of(BMC_PWM) is None
    assert Hardware(fake_dev, tmp_path / "nothing", bmc=bmc.hwmon).hwmons()[0].hwmon == "ipmi"
    await bmc.close()


def test_settings_take_the_bmc_output() -> None:
    """The settings accept the BMC output, and never pass it to hddfancontrol."""
    settings = from_dict({"drives": ["/dev/sda"], "pwms": [{"path": BMC_PWM, "start": 38, "stop": 38}]})
    assert settings.pwms[0].path == BMC_PWM
    args = hddfancontrol_args(Settings(drives=("/dev/sda",), pwms=(Pwm(BMC_PWM, 38, 38),)), None)
    assert args[args.index("--pwm") + 1 : args.index("--drive-temp-range")] == []
