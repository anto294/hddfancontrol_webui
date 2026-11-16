"""Shared fixtures: a fake /sys and /dev, and fake programs."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ScriptMaker = Callable[[str, str], str]


@pytest.fixture
def fake_sys(tmp_path: Path) -> Path:
    """A /sys with one fan chip (two PWM outputs, one temperature) and two drives."""
    root = tmp_path / "sys"
    hwmon = root / "class" / "hwmon" / "hwmon2"
    hwmon.mkdir(parents=True)
    files = {
        "name": "nct6798",
        "pwm1": "128",
        "pwm1_enable": "5",
        "fan1_input": "812",
        "pwm2": "255",
        "pwm2_enable": "1",
        "temp1_input": "41500",
        "temp1_label": "SYSTIN",
        "temp2_input": "38000",
    }
    for name, value in files.items():
        (hwmon / name).write_text(value + "\n")
    # The chip itself, which keeps its name when the hwmon number changes.
    chip = root / "devices" / "platform" / "nct6775.656"
    chip.mkdir(parents=True)
    (hwmon / "device").symlink_to("../../../devices/platform/nct6775.656")
    # A chip without fans, sorted after hwmon2 by number, not by text.
    other = root / "class" / "hwmon" / "hwmon10"
    other.mkdir()
    (other / "name").write_text("acpitz\n")
    (other / "temp1_input").write_text("27800\n")
    for name, model, size, rotational in (("sda", "ST4000VN006", 7814037168, 1), ("nvme0n1", "Samsung 980", 1953525168, 0)):
        block = root / "class" / "block" / name
        (block / "device").mkdir(parents=True)
        (block / "queue").mkdir()
        (block / "device" / "model").write_text(model + "\n")
        (block / "size").write_text(f"{size}\n")
        (block / "queue" / "rotational").write_text(f"{rotational}\n")
    (root / "module" / "i2c_dev").mkdir(parents=True)
    return root


@pytest.fixture
def fake_dev(tmp_path: Path) -> Path:
    """A /dev with stable names for two drives and a partition."""
    root = tmp_path / "dev"
    by_id = root / "disk" / "by-id"
    by_id.mkdir(parents=True)
    for name in ("sda", "sda1", "nvme0n1"):
        (root / name).write_text("")
    links = {
        "ata-ST4000VN006-3CW104_ZW627CWW": "sda",
        "wwn-0x5000c500e0000001": "sda",
        "ata-ST4000VN006-3CW104_ZW627CWW-part1": "sda1",
        "nvme-Samsung_980_S64": "nvme0n1",
        "nvme-eui.002538": "nvme0n1",
    }
    for link, target in links.items():
        (by_id / link).symlink_to(f"../../{target}")
    return root


@pytest.fixture
def make_script(tmp_path: Path) -> ScriptMaker:
    """Write an executable Python script that stands in for a program."""
    directory = tmp_path / "bin"
    directory.mkdir()

    def make(name: str, body: str) -> str:
        path = directory / name
        path.write_text(f"#!{sys.executable}\nimport sys, time, os\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    return make


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the variables of the machine running the tests out of them."""
    for name in list(os.environ):
        if name.startswith("WEBUI_") or name in ("DRIVES", "PWM", "INTERVAL", "CONTROL_MODE"):
            monkeypatch.delenv(name, raising=False)
