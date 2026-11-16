"""Finding and loading the sensor chip modules."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ScriptMaker

from hddfancontrol_webui import sensors
from hddfancontrol_webui.sensors import (
    STATUS_ALREADY_LOADED,
    STATUS_FAILED,
    STATUS_LOADED,
    chip_modules,
    is_loaded,
    scan_sensor_modules,
)

DETECT_OUTPUT = """
Driver `nct6775':
  * ISA bus, address 0x290
    Chip `Nuvoton NCT6798D Super IO Sensors' (confidence: 9)

To load everything that is needed, add this to /etc/modules:
#----cut here----
# Chip drivers
modprobe coretemp
modprobe nct6775
modprobe nct6775
modprobe evil; rm -rf /
#----cut here----
"""


def test_chip_modules() -> None:
    """Only well-formed module names of the Chip drivers section, once each."""
    assert chip_modules(DETECT_OUTPUT) == ["coretemp", "nct6775"]
    assert chip_modules("nothing here") == []


def test_is_loaded(fake_sys: Path) -> None:
    """/sys/module uses underscores."""
    assert is_loaded("i2c-dev", fake_sys)
    assert not is_loaded("nct6775", fake_sys)


async def test_scan_loads_missing_modules(fake_sys: Path, make_script: ScriptMaker) -> None:
    """Loaded modules are skipped, missing ones are loaded, failures are reported."""
    (fake_sys / "module" / "coretemp").mkdir()
    detect = make_script("sensors-detect", f"print({DETECT_OUTPUT!r})")
    modprobe = make_script(
        "modprobe",
        "if sys.argv[1] == 'nct6775':\n"
        "    print('modprobe: FATAL: Module nct6775 not found in directory /lib/modules/6.12.15-production+truenas')\n"
        "    sys.exit(1)\n",
    )
    lines: list[str] = []
    report = await scan_sensor_modules(lines.append, detect, modprobe, fake_sys)
    statuses = {m.module: m.status for m in report.modules}
    assert statuses == {"i2c-dev": STATUS_ALREADY_LOADED, "coretemp": STATUS_ALREADY_LOADED, "nct6775": STATUS_FAILED}
    assert report.failed == ["nct6775"]
    assert "not found" in report.modules[-1].error
    assert report.state == "done"
    assert report.to_dict()["failed"] == ["nct6775"]
    assert any(line.startswith("WARNING: cannot load module nct6775") for line in lines)


async def test_scan_loads_module(fake_sys: Path, make_script: ScriptMaker) -> None:
    """A module that modprobe loads is reported as loaded."""
    detect = make_script("sensors-detect", "print('#----cut here----\\n# Chip drivers\\nmodprobe it87\\n#----cut here----')")
    modprobe = make_script("modprobe", "")
    report = await scan_sensor_modules(lambda _: None, detect, modprobe, fake_sys)
    assert [(m.module, m.status) for m in report.modules] == [("i2c-dev", STATUS_ALREADY_LOADED), ("it87", STATUS_LOADED)]


async def test_scan_without_programs(tmp_path: Path) -> None:
    """Missing programs are reported, never raised."""
    lines: list[str] = []
    report = await scan_sensor_modules(lines.append, str(tmp_path / "none"), str(tmp_path / "none"), tmp_path)
    assert report.modules[0].status == STATUS_FAILED
    assert report.detect_error
    assert report.state == "done"


async def test_scan_finds_nothing(fake_sys: Path, make_script: ScriptMaker) -> None:
    """No chip driver is not an error."""
    detect = make_script("sensors-detect", "print('Sorry, no sensors were detected.'); sys.exit(1)")
    lines: list[str] = []
    report = await scan_sensor_modules(lines.append, detect, make_script("modprobe", ""), fake_sys)
    assert report.failed == []
    assert any("found no chip driver" in line for line in lines)


async def test_run_kills_a_program_that_hangs(make_script: ScriptMaker) -> None:
    """A program that does not finish in time is killed and reported as an OSError."""
    with pytest.raises(OSError, match="did not finish"):
        await sensors._run(make_script("hung", "time.sleep(30)"), limit_s=0.2)
