"""Recording the history and refreshing the SMART data."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui import monitor as monitor_module
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.history import History
from hddfancontrol_webui.monitor import Monitor
from hddfancontrol_webui.settings import Settings, from_dict
from hddfancontrol_webui.templog import TempLog

DRIVE = "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
RECENT = (datetime.now(UTC) - timedelta(hours=1)).isoformat()


def make_monitor(tmp_path: Path, fake_sys: Path, fake_dev: Path, settings: Settings, reader: Any = None) -> Monitor:
    """A monitor on the fake trees with a temperature log."""
    log = tmp_path / "temps.jsonl"
    log.write_text(
        json.dumps(
            {
                "time_utc": RECENT,
                "measures": [{"device": "sda", "temp_celcius": 40.0}, {"device": "sdb", "temp_celcius": None}],
            }
        )
        + "\n"
    )
    history = History(tmp_path / "history.db")
    kwargs = {"smart_reader": reader} if reader else {}
    return Monitor(history, TempLog(log), Hardware(fake_dev, fake_sys), lambda: settings, **kwargs)


def test_record_once(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """Temperatures are stored once per sample; fan PWM and RPM every time."""
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, Settings())
    assert monitor.record_once() == 1 + 3  # sda, then pwm1 %, pwm1 RPM, pwm2 %
    assert monitor.record_once() == 3
    data = monitor.history.query(24 * 365)
    kinds = {(s["kind"], s["name"]) for s in data["series"]}
    assert ("temp", "sda") in kinds and ("rpm", "/sys/class/hwmon/hwmon2/pwm1") in kinds
    assert ("temp", "sdb") not in kinds
    monitor.history.close()


async def test_smart_reads_only_monitored_drives(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """Only the monitored drives are read and shown; nothing is read when none is."""
    read: list[str] = []

    async def reader(device: str) -> dict[str, Any]:
        read.append(device)
        return {"status": "ok"}

    settings = from_dict({"drives": ["nvme"], "monitor_drives": [DRIVE]})
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings, reader)
    monitor.history.save_smart("/dev/disk/by-id/ata-Old", {"status": "ok"})
    smart = await monitor.refresh_smart()
    assert list(smart) == [DRIVE]
    assert read == [DRIVE]
    monitor._settings = Settings
    smart = await monitor.refresh_smart()
    assert smart == {} and read == [DRIVE]
    monitor.history.close()


async def test_refresh_smart_keeps_data_of_sleeping_drives(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A drive asleep at the refresh keeps its last data, marked as asleep."""
    answers = [{"status": "ok", "model": "X"}, {"status": "standby"}]

    async def reader(device: str) -> dict[str, Any]:
        return answers.pop(0)

    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({"drives": [DRIVE]}), reader)
    first = await monitor.refresh_smart()
    assert first[DRIVE]["status"] == "ok"
    second = await monitor.refresh_smart()
    assert second[DRIVE]["model"] == "X" and second[DRIVE]["asleep"] is True
    assert second[DRIVE]["time"] == first[DRIVE]["time"]
    monitor.history.close()


async def test_loops_start_and_stop(tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The background loops record, purge, read SMART and stop cleanly."""
    real = asyncio.sleep

    async def quick(_: float) -> None:
        await real(0.01)

    calls: list[str] = []

    async def reader(device: str) -> dict[str, Any]:
        calls.append(device)
        return {"status": "ok"}

    monkeypatch.setattr(monitor_module.asyncio, "sleep", quick)
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({"drives": [DRIVE]}), reader)
    monitor.start()
    await real(0.1)
    await monitor.stop()
    assert calls and monitor.history.query(24 * 365)["series"]
    monitor.history.close()

    idle = make_monitor(tmp_path / "b", fake_sys, fake_dev, from_dict({"smart_interval_h": 0}), reader)
    calls.clear()
    idle.start()
    await real(0.05)
    await idle.stop()
    idle.history.close()
    assert calls == []


@pytest.fixture(autouse=True)
def _dirs(tmp_path: Path) -> None:
    """Directories the second monitor needs."""
    (tmp_path / "b").mkdir()
