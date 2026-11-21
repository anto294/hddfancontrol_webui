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
    PROFILES,
    STALE_S,
    Bmc,
    BmcConfigError,
    Reading,
    catalogue,
    check_config,
    curve_percent,
    detect_model,
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

    def __init__(self, folder: Path, info: str, sensors: str, fru: str = "") -> None:
        """Write the program and its answers in folder."""
        self.folder = folder
        self.calls = folder / "calls"
        (folder / "mcinfo").write_text(info)
        (folder / "sensor").write_text(sensors)
        (folder / "fru").write_text(fru)
        self.program = folder / "ipmitool"
        self.program.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.calls}"\n'
            'case "$1" in\n'
            f'  mc) cat "{folder}/mcinfo" ;;\n'
            f'  fru) if [ ! -s "{folder}/fru" ]; then echo "no FRU" >&2; exit 1; fi; cat "{folder}/fru" ;;\n'
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


DELL_FRU = """FRU Device Description : Builtin FRU Device (ID 0)
 Board Mfg             : DELL
 Board Product         : PowerEdge R730xd
 Product Manufacturer  : DELL
 Product Name          : PowerEdge R730xd
"""
WEBUI = {"control": "webui", "profile": "normal", "min_percent": 20}


def make(
    tmp_path: Path,
    info: str = DELL_INFO,
    sensors: str = DELL_SENSORS,
    fru: str = DELL_FRU,
    cpu: list[float | None] | None = None,
    group: list[bool] | None = None,
) -> tuple[Bmc, FakeIpmi, list[str]]:
    """A Bmc on a fake ipmitool, and the lines it logged; cpu[0] is the host's processor temperature, group[0] whether BMC_PWM has a fan group."""
    fake = FakeIpmi(tmp_path, info, sensors, fru)
    logged: list[str] = []
    temps = cpu if cpu is not None else [40.0]
    grouped = group if group is not None else [False]
    bmc = Bmc(str(fake.program), fake.device, logged.append, cpu_temp=lambda: temps[0], in_group=lambda: grouped[0])
    return bmc, fake, logged


async def started(tmp_path: Path, config: dict[str, Any] | None = None, **kwargs: Any) -> tuple[Bmc, FakeIpmi, list[str]]:
    """A started Bmc with this configuration, its first commands taken."""
    bmc, fake, logged = make(tmp_path, **kwargs)
    await bmc.start()
    if config is not None:
        bmc.set_config(config)
    fake.take_calls()
    return bmc, fake, logged


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


async def test_dell_found_and_left_to_the_idrac(tmp_path: Path) -> None:
    """A Dell gets its fans back at start; by default the iDRAC keeps them, whatever the fan controller asks."""
    bmc, fake, logged = make(tmp_path)
    await bmc.start()
    try:
        assert bmc.present and bmc.can_drive and not bmc.drives_fans and bmc.name == "iDRAC"
        assert (bmc.product, bmc.firmware, bmc.detected) == ("PowerEdge R730xd", "2.86", "dell-r730xd")
        assert fake.take_calls() == ["mc info", "fru print 0", "raw 0x30 0x30 0x01 0x01", "sensor"]
        assert logged[0] == "BMC found: iDRAC 2.86 on PowerEdge R730xd, its fans can be driven (experimental)"
        device = bmc.hwmon()
        assert device is not None and device.name == "iDRAC"
        assert [(p.path, p.value, p.enable, p.rpm) for p in device.pwms] == [(BMC_PWM, None, 2, 3540)]
        assert [t.label for t in device.temps] == ["Inlet Temp", "Exhaust Temp", "Temp"]
        bmc.write(128)
        await bmc._apply()
        assert fake.take_calls() == [] and not bmc.state.manual
    finally:
        await bmc.close()


async def test_dell_follows_the_cpu_and_the_drives(tmp_path: Path) -> None:
    """The web UI runs the fans at the highest of the minimum, the CPU curve and the drives."""
    cpu: list[float | None] = [40.0]
    bmc, fake, _ = await started(tmp_path, WEBUI, cpu=cpu)
    try:
        await bmc._apply()
        # 40 °C is under the normal curve: the minimum speed, 20 %.
        assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x02 0xff 0x14"]
        await bmc._apply()
        assert fake.take_calls() == []  # nothing new to send
        cpu[0] = 65.0
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x02 0xff 0x26"]  # 37.5 % -> 38 %
        bmc.write(128)
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x02 0xff 0x32"]  # the drives ask more: 50 %
        status = bmc.status()
        assert status is not None and status["manual"] and status["drives_fans"]
        assert (status["percent"], status["cpu_percent"], status["drives_percent"], status["cpu_temp"]) == (50, 38, 50, 65.0)
        device = bmc.hwmon()
        assert device is not None and (device.pwms[0].value, device.pwms[0].enable) == (128, 1)
        bmc.write(255)
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x02 0xff 0x64"]
        # Without a fan group the CPU curve goes on when the controller lets go,
        # and it comes down slowly.
        bmc.release()
        cpu[0] = 30.0
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x02 0xff 0x24"] and bmc.state.cpu_percent == 36.5
        # Back to the iDRAC when the web UI no longer drives them.
        bmc.set_config({**WEBUI, "control": "bmc"})
        await bmc._apply()
        assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"] and not bmc.state.manual
    finally:
        await bmc.close()


async def test_dell_in_a_fan_group_goes_back_when_released(tmp_path: Path) -> None:
    """Fans in a fan group are the iDRAC's while the fan controller does not drive them."""
    group = [True]
    bmc, fake, logged = await started(tmp_path, WEBUI, group=group)
    await bmc._apply()
    assert fake.take_calls() == [] and bmc.state.handback is None
    bmc.write(100)
    await bmc._apply()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x02 0xff 0x27"]
    bmc.release()
    await bmc._apply()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"]
    group[0] = False
    await bmc._apply()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x02 0xff 0x14"]
    await bmc.close()
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x01"]
    assert not any("go back" in line for line in logged)


async def test_dell_cpu_temperature_from_the_bmc(tmp_path: Path) -> None:
    """Without a processor temperature on the host, the BMC's; without either, the iDRAC drives."""
    cpu: list[float | None] = [None]
    bmc, fake, logged = await started(tmp_path, {**WEBUI, "profile": "turbo"}, cpu=cpu)
    await bmc._apply()
    assert bmc.state.cpu_temp == 48.0 and fake.take_calls()[-1] == "raw 0x30 0x30 0x02 0xff 0x21"  # turbo at 48 °C: 33 %
    (fake.folder / "sensor").write_text("\n".join(line for line in DELL_SENSORS.splitlines() if not line.startswith("Temp ")))
    await bmc._poll()
    await bmc._apply()
    assert bmc.state.handback == "no_cpu_temp" and fake.take_calls() == ["sensor", "raw 0x30 0x30 0x01 0x01"]
    assert "go back to the iDRAC: no_cpu_temp" in logged[-1]

    def broken() -> float | None:
        raise OSError("gone")

    bmc.host_cpu_temp = broken
    assert bmc._cpu_temp() is None
    cpu[0] = 50.0
    bmc.host_cpu_temp = lambda: cpu[0]
    await bmc._apply()
    assert bmc.state.handback is None and "drives the fans again (no_cpu_temp is over)" in logged[-1]
    await bmc.close()


async def test_dell_hands_back_when_hot(tmp_path: Path) -> None:
    """A temperature near its limit gives the fans back to the iDRAC, but never full speed; they come back cooler."""
    hot = DELL_SENSORS.replace("Inlet Temp       | 23.000", "Inlet Temp       | 39.000")
    bmc, fake, logged = await started(tmp_path, WEBUI, sensors=hot)
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
    bmc.set_config(WEBUI)
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
    bmc, fake, logged = await started(tmp_path, WEBUI)
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
    """An iLO shows its temperatures and fans; no command is ever sent to its fans, whatever the configuration."""
    bmc, fake, logged = make(tmp_path, HPE_INFO, HPE_SENSORS, fru="Product Name : ProLiant ML350 Gen9\n")
    await bmc.start()
    assert bmc.present and not bmc.can_drive and bmc.name == "iLO" and bmc.detected == "hpe-ml350-gen9"
    assert "read only" in logged[0]
    bmc.set_config({**WEBUI, "model": "hpe-ml350-gen9"})
    with pytest.raises(BmcConfigError) as refused:
        bmc.set_config({**WEBUI, "model": "dell-r730"})
    assert refused.value.problems[0]["code"] == "bmc_model_vendor"
    bmc.write(100)
    await bmc._apply()
    await bmc.close()
    assert fake.take_calls() == ["mc info", "fru print 0", "sensor"]
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
    bmc.set_config(WEBUI)
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
    # The monitor reads only the fans: the BMC keeps its output, without its temperatures.
    full, light = hardware.hwmons()[-1], hardware.hwmons(temps=False)[-1]
    assert full.temps and light.temps == [] and light.pwms == full.pwms and light.name == full.name
    await bmc.close()


def test_settings_take_the_bmc_output() -> None:
    """The settings accept the BMC output, and never pass it to hddfancontrol."""
    settings = from_dict({"drives": ["/dev/sda"], "pwms": [{"path": BMC_PWM, "start": 38, "stop": 38}]})
    assert settings.pwms[0].path == BMC_PWM
    args = hddfancontrol_args(Settings(drives=("/dev/sda",), pwms=(Pwm(BMC_PWM, 38, 38),)), None)
    assert args[args.index("--pwm") + 1 : args.index("--drive-temp-range")] == []


def test_check_config() -> None:
    """The configuration of the IPMI tab: known values, a curve warmer and not slower at each point."""
    config = check_config(
        {"model": "dell-r730", "control": "webui", "profile": "custom", "curve": [[40, 10], [70, 100]], "min_percent": 15}
    )
    assert config.points() == ((40.0, 10.0), (70.0, 100.0)) and config.to_dict()["curve"] == [[40.0, 10.0], [70.0, 100.0]]
    assert check_config({}).points() == PROFILES["normal"]
    with pytest.raises(BmcConfigError):
        check_config([])

    def codes(data: dict[str, Any]) -> list[tuple[str, str]]:
        with pytest.raises(BmcConfigError) as refused:
            check_config(data)
        return [(p["field"], p["code"]) for p in refused.value.problems]

    assert codes({"model": "x", "control": "y", "profile": "z", "min_percent": 2}) == [
        ("model", "invalid_choice"),
        ("control", "invalid_choice"),
        ("profile", "invalid_choice"),
        ("min_percent", "out_of_range"),
    ]
    assert codes({"min_percent": True}) == [("min_percent", "out_of_range")]
    assert codes({"curve": [[40, 10]]}) == [("curve", "curve_points")]
    assert codes({"curve": "no"}) == [("curve", "curve_points")]
    assert codes({"curve": [[40, 10], [50]]}) == [("curve.1", "invalid_document")]
    assert codes({"curve": [[40, 10], [500, 20]]}) == [("curve.1.0", "out_of_range")]
    assert codes({"curve": [[40, 10], [50, "a"]]}) == [("curve.1.1", "out_of_range")]
    assert codes({"curve": [[40, 10], [40, 20]]}) == [("curve.1", "curve_order")]
    assert codes({"curve": [[40, 50], [60, 20]]}) == [("curve.1", "curve_order")]


def test_curve_and_models() -> None:
    """Straight lines between the points and flat outside; the model from the FRU product name."""
    points = ((40.0, 20.0), (60.0, 60.0), (80.0, 100.0))
    assert [curve_percent(points, t) for t in (30, 40, 50, 70, 90)] == [20, 20, 40, 80, 100]
    assert detect_model(DELL, "PowerEdge R730xd") == "dell-r730xd"
    assert detect_model(DELL, "PowerEdge R730") == "dell-r730"
    assert detect_model(DELL, "PowerEdge R940") == "dell-other"
    assert detect_model(HPE, "ProLiant ML350 Gen10") == "hpe-ml350-gen10"
    assert detect_model(HPE, "") == "hpe-other"
    assert detect_model(None, "PowerEdge R730") == "other"
    data = catalogue()
    assert {m["id"] for m in data["models"] if m["control"]} <= {m["id"] for m in data["models"] if m["vendor"] == DELL}
    assert set(data["profiles"]) == {"silent", "normal", "turbo"}


async def test_config_is_kept(tmp_path: Path) -> None:
    """bmc.json keeps the configuration across a restart; a bad file leaves the fans to the iDRAC."""
    fake = FakeIpmi(tmp_path, DELL_INFO, DELL_SENSORS, DELL_FRU)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    first = Bmc(str(fake.program), fake.device, config_dir=config_dir)
    assert first.describe() is None
    await first.start()
    first.set_config({**WEBUI, "model": "dell-r730", "min_percent": 30})
    data = first.describe()
    assert data is not None and data["detected"] == "dell-r730xd" and data["vendor"] == DELL
    assert data["config"]["model"] == "dell-r730" and data["models"]
    await first.close()
    assert Bmc(str(fake.program), fake.device, config_dir=config_dir).config.min_percent == 30
    (config_dir / "bmc.json").write_text("{bad")
    logged: list[str] = []
    assert Bmc(str(fake.program), fake.device, logged.append, config_dir=config_dir).config.control == "bmc"
    assert "cannot read" in logged[0]


def test_config_of_the_wrong_type() -> None:
    """A model, control or profile that is no string is a problem, never a crash."""
    with pytest.raises(BmcConfigError) as refused:
        check_config({"model": [], "control": {}, "profile": ["custom"]})
    assert [p["field"] for p in refused.value.problems] == ["model", "control", "profile"]


async def test_the_loop_survives_a_bug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected error in the loop gives the fans back to the iDRAC and the loop goes on."""
    bmc, fake, logged = await started(tmp_path, WEBUI)
    monkeypatch.setattr(bmc_module, "APPLY_S", 0.01)
    bmc.write(100)
    await bmc._apply()
    assert bmc.state.manual
    fake.take_calls()
    failures = [0]

    def broken(_desired: int | None, _cpu: float | None) -> tuple[int | None, str | None]:
        failures[0] += 1
        raise RuntimeError("bug")

    monkeypatch.setattr(bmc, "_target", broken)
    task = asyncio.create_task(bmc._loop())
    for _ in range(100):
        if failures[0] >= 2:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert failures[0] >= 2 and not bmc.state.manual
    assert "raw 0x30 0x30 0x01 0x01" in fake.take_calls() and "unexpected error" in logged[-1]
    await bmc.close()


async def test_a_cancelled_command_ends_first(tmp_path: Path) -> None:
    """A command cancelled half way still ends before the next one, so the fans go back to the iDRAC last."""
    bmc, fake, _ = make(tmp_path)
    # Only the manual command is slow: without waiting for it, DELL_AUTO would land first.
    fake.program.write_text(f'#!/bin/sh\ncase "$*" in *0x00) sleep 0.3 ;; esac\necho "$*" >> "{fake.calls}"\n')
    task = asyncio.create_task(bmc._run(*bmc_module.DELL_MANUAL))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await bmc._run(*bmc_module.DELL_AUTO)
    assert fake.take_calls() == ["raw 0x30 0x30 0x01 0x00", "raw 0x30 0x30 0x01 0x01"]


async def test_no_cpu_state_while_the_idrac_drives(tmp_path: Path) -> None:
    """Back with the iDRAC, the CPU curve forgets its level, and no "drives again" line is logged."""
    bmc, fake, logged = await started(tmp_path, WEBUI, cpu=[None])
    (fake.folder / "sensor").write_text("Fan1 | 3600.000 | RPM | ok\n")
    await bmc._poll()
    await bmc._apply()
    assert bmc.state.handback == "no_cpu_temp"
    bmc.set_config({**WEBUI, "control": "bmc"})
    bmc.state.cpu_percent = 80.0
    await bmc._apply()
    assert bmc.state.handback is None and bmc.state.cpu_percent is None
    assert not any("drives the fans again" in line for line in logged)
    await bmc.close()


async def test_report_for_the_support_request(tmp_path: Path) -> None:
    """The support request gets the BMC, the server, the configuration, who drives the fans and every sensor."""
    bmc, _, _ = make(tmp_path)
    assert bmc.report() == ""  # no BMC found yet
    await bmc.start()
    try:
        lines = bmc.report().splitlines()
        assert lines[:5] == [
            "== bmc, as the web UI sees it",
            "iDRAC 2.86 on PowerEdge R730xd",
            "model: dell-r730xd (found: dell-r730xd), fans can be driven",
            "bmc.json: control bmc, minimum 20 %, profile normal: 45 °C 0 %, 60 °C 25 %, 70 °C 50 %, 80 °C 100 %",
            "now: the iDRAC drives the fans",
        ]
        assert "Inlet Temp       | 23       | degrees C | ok   | limit 42" in lines
        assert "Fan1             | 3600     | RPM       | ok   | limit na" in lines
        bmc.set_config(WEBUI)
        await bmc._apply()
        assert "now: the web UI drives the fans at 20 %" in bmc.report()
        bmc.state.error = "boom"
        bmc.state.manual = False
        bmc.state.handback = "the sensors cannot be read"
        assert "now: the iDRAC drives the fans (the sensors cannot be read)\nerror: boom" in bmc.report()
    finally:
        await bmc.close()


async def test_report_of_a_read_only_bmc(tmp_path: Path) -> None:
    """An iLO is reported read only, with its fan duties in percent and the readings it lacks as na."""
    bmc, _, _ = make(tmp_path, HPE_INFO, HPE_SENSORS, fru="")
    await bmc.start()
    report = bmc.report()
    await bmc.close()
    assert report.splitlines()[1:3] == ["iLO on iLO 4", "model: hpe-other (found: hpe-other), fans read only"]
    assert "Fan 2            | na       | percent   | na   | limit na" in report
