"""The checks at start: permissions, devices, capabilities and programs, on fake trees."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import ScriptMaker

from hddfancontrol_webui import checks
from hddfancontrol_webui.checks import CAP_SYS_ADMIN, CAP_SYS_MODULE, CAP_SYS_RAWIO, message, run_checks, startup_checks
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.settings import from_dict

DRIVE = "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
PWM1 = "/sys/class/hwmon/hwmon2/pwm1"
ALL_CAPS = (1 << CAP_SYS_MODULE) | (1 << CAP_SYS_RAWIO) | (1 << CAP_SYS_ADMIN)

skip_as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root opens and writes files whatever their mode")


def fake_proc(tmp_path: Path, caps: int) -> Path:
    """A /proc whose self/status has these effective capabilities."""
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    (root / "self" / "status").write_text(f"Name:\tpython\nCapPrm:\t000001ffffffffff\nCapEff:\t{caps:016x}\n")
    return root


def codes(problems: list[dict[str, object]]) -> list[str]:
    """The codes of the problems, in order."""
    return [str(p["code"]) for p in problems]


def test_a_good_host_has_no_problem(
    tmp_path: Path, fake_dev: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root with its capabilities, a writable /config, readable drives, writable fans and every program: nothing."""
    hardware = Hardware(fake_dev, fake_sys, fake_proc(tmp_path, ALL_CAPS))
    settings = from_dict(
        {"drives": [DRIVE, "ata"], "monitor_drives": [DRIVE], "pwms": [{"path": PWM1, "start": 80, "stop": 40}]}
    )
    programs = {name: make_script(name, "") for name in ("hddfancontrol", "hd-idle", "smartctl", "sensors-detect", "modprobe")}
    assert startup_checks(tmp_path / "config", hardware, settings, programs, load_modules=True, euid=0) == []
    monkeypatch.setattr(checks.os, "geteuid", lambda: 0)
    lines: list[str] = []
    assert run_checks(lines.append, tmp_path / "config", hardware, settings, programs, True) == []
    assert lines == ["Startup checks: permissions, devices and programs are fine"]


@skip_as_root
def test_every_problem_is_found(tmp_path: Path, fake_dev: Path, fake_sys: Path) -> None:
    """Each problem gets its code, its parameters and a log line that says what to do."""
    hardware = Hardware(fake_dev, fake_sys, fake_proc(tmp_path, 1 << CAP_SYS_ADMIN))
    settings = from_dict(
        {
            "drives": [DRIVE],
            "monitor_drives": [DRIVE, "/dev/disk/by-id/ata-GONE"],
            "pwms": [{"path": PWM1, "start": 80, "stop": 40}],
        }
    )
    (fake_dev / "sda").chmod(0)
    (fake_sys / "class" / "hwmon" / "hwmon2" / "pwm1").chmod(0o444)
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(0o500)
    try:
        problems = startup_checks(config, hardware, settings, {"smartctl": "no-such-smartctl"}, load_modules=True, euid=0)
    finally:
        config.chmod(0o700)
    assert codes(problems) == [
        "config_not_writable",
        "drive_not_readable",
        "missing_capabilities",
        "pwm_not_writable",
        "program_missing",
    ]
    by_code = {p["code"]: p["params"] for p in problems}
    assert by_code["drive_not_readable"]["drive"] == DRIVE
    assert by_code["missing_capabilities"]["caps"] == "CAP_SYS_RAWIO, CAP_SYS_MODULE"
    assert by_code["pwm_not_writable"]["path"] == PWM1
    assert by_code["program_missing"] == {"program": "smartctl", "what": "SMART"}
    assert all(message(p) for p in problems)


def test_without_root_or_drives(tmp_path: Path, fake_sys: Path) -> None:
    """Not root: said once, and neither capabilities nor fans are looked at; an empty /dev is no_drives."""
    hardware = Hardware(tmp_path / "nodev", fake_sys, fake_proc(tmp_path, 0))
    settings = from_dict({"pwms": [{"path": PWM1, "start": 80, "stop": 40}]})
    problems = startup_checks(tmp_path / "config", hardware, settings, {"modprobe": "no-such"}, load_modules=False, euid=1000)
    assert codes(problems) == ["not_root", "no_drives"]
    assert "uid 1000" in message(problems[0])


def test_unreadable_capabilities_and_sys(
    tmp_path: Path, fake_dev: Path, fake_sys: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No /proc/self/status: capabilities are not judged; /sys read only: the fans are its own banner's."""
    hardware = Hardware(fake_dev, fake_sys, tmp_path / "noproc")
    monkeypatch.setattr(hardware, "read_only", lambda: True)
    (fake_sys / "class" / "hwmon" / "hwmon2" / "pwm1").chmod(0o444)
    settings = from_dict({"pwms": [{"path": PWM1, "start": 80, "stop": 40}]})
    assert startup_checks(tmp_path / "config", hardware, settings, {}, load_modules=True, euid=0) == []
    assert hardware.writable("/sys/class/hwmon/hwmon9/pwm1") is None
    assert hardware.open_error("/dev/disk/by-id/ata-GONE") is None


def test_run_checks_logs_and_never_raises(tmp_path: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each problem is a WARNING line; a check that breaks is logged and gives no problem."""
    hardware = Hardware(fake_dev, tmp_path / "nosys", fake_proc(tmp_path, ALL_CAPS))
    lines: list[str] = []
    monkeypatch.setattr(checks, "startup_checks", lambda *args: [{"code": "not_root", "params": {"uid": "5"}}])
    assert codes(run_checks(lines.append, tmp_path, hardware, from_dict({}), {}, False)) == ["not_root"]
    assert lines == ["WARNING: startup check: " + message({"code": "not_root", "params": {"uid": "5"}})]

    def broken(*args: object) -> list[dict[str, object]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(checks, "startup_checks", broken)
    lines.clear()
    assert run_checks(lines.append, tmp_path, hardware, from_dict({}), {}, False) == []
    assert "boom" in lines[0]
