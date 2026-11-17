"""Recording the history and refreshing the SMART data."""

from __future__ import annotations

import asyncio
import json
import time
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
    # sda is kept under its wwn- name, which a reboot does not change.
    assert ("temp", "/dev/disk/by-id/wwn-0x5000c500e0000001") in kinds
    assert ("rpm", "/sys/class/hwmon/hwmon2/pwm1") in kinds
    assert not any(kind == "temp" and "sdb" in name for kind, name in kinds)
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


def test_temperature_summary(tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One line with the controlled drives, the chosen fans and the drives only monitored."""
    other = "/dev/disk/by-id/nvme-Other"
    settings = from_dict(
        {
            "drives": [DRIVE, "/dev/disk/by-id/ata-Second"],
            "monitor_drives": [DRIVE, other],
            "pwms": [{"path": "/sys/class/hwmon/hwmon2/pwm1", "start": 200, "stop": 75}],
        }
    )
    lines: list[str] = []
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    monitor._log = lines.append
    monitor.history.save_smart(other, {"status": "ok", "temperature": 38})
    monitor.record_once()
    summary = monitor.summary()
    assert summary is not None
    assert summary[1] == (
        "controlled: sda 40°C, sdb asleep (hottest sda 40°C) | fans: pwm1 50% 812 RPM | monitored (SMART): nvme-Other 38°C"
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(monitor_module.time, "monotonic", lambda: clock["now"])
    monitor._log_summary(summary)
    monitor._log_summary(summary)
    assert len(lines) == 1  # nothing changed
    clock["now"] += monitor_module.TEMPS_HEARTBEAT
    monitor._log_summary(summary)
    assert len(lines) == 2  # still alive
    monitor._log_summary(((("sda", 41),), "changed"))
    assert lines[-1] == "changed"
    monitor._log_summary(None)
    assert len(lines) == 3

    (tmp_path / "empty").mkdir()
    empty = make_monitor(tmp_path / "empty", fake_sys, fake_dev, Settings())
    assert empty.summary() is None
    empty.history.close()
    monitor.history.close()


async def test_loops_survive_errors(tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing database or SMART read is logged once; the loops go on, and the alerts still run."""
    real = asyncio.sleep

    async def quick(_: float) -> None:
        await real(0.01)

    async def reader(device: str) -> dict[str, Any]:
        raise RuntimeError("smartctl broke")

    monkeypatch.setattr(monitor_module.asyncio, "sleep", quick)
    lines: list[str] = []
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({"drives": [DRIVE]}), reader)
    monitor._log = lines.append
    updates: list[Any] = []

    class FakeAlerts:
        active: dict[str, Any] = {}

        async def update(self, conditions: Any) -> None:
            updates.append(conditions)

    monitor.alerts = FakeAlerts()  # type: ignore[assignment]  # only update and active are used
    broken = {"on": True}
    record = monitor.history.record

    def failing_record(*args: Any) -> None:
        if broken["on"]:
            raise OSError("disk full")
        record(*args)

    monkeypatch.setattr(monitor.history, "record", failing_record)
    monitor.start()
    await real(0.1)
    assert all(not task.done() for task in monitor._tasks)
    assert sum("recording failed" in line for line in lines) == 1
    assert sum("SMART refresh failed" in line for line in lines) == 1
    assert len(updates) > 1  # the alerts ran although recording failed
    broken["on"] = False
    await real(0.1)
    assert any("recording works again" in line for line in lines)
    await monitor.stop()
    monitor.history.close()


async def test_smart_interval_change_applies_at_once(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shorter SMART interval applies at the next check, not after the old wait."""
    real = asyncio.sleep
    clock = {"now": 0.0}

    async def tick(_: float) -> None:
        clock["now"] += 60
        await real(0.005)

    reads: list[float] = []

    async def reader(device: str) -> dict[str, Any]:
        reads.append(clock["now"])
        return {"status": "ok"}

    monkeypatch.setattr(monitor_module.asyncio, "sleep", tick)
    # Only the monitor's clock: the event loop keeps the real one.
    fake_time = type("FakeTime", (), {"monotonic": staticmethod(lambda: clock["now"]), "time": staticmethod(time.time)})
    monkeypatch.setattr(monitor_module, "time", fake_time)
    settings = {"value": from_dict({"drives": [DRIVE], "smart_interval_h": 168})}
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings["value"], reader)
    monitor._settings = lambda: settings["value"]
    task = asyncio.create_task(monitor._smart_loop())
    while not reads:
        await real(0.005)
    assert reads == [60.0]
    settings["value"] = from_dict({"drives": [DRIVE], "smart_interval_h": 1})
    while len(reads) < 2:
        await real(0.005)
    task.cancel()
    assert reads[1] - reads[0] == 3600
    monitor.history.close()


def test_summary(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """The summary is due daily or weekly at 9 local time, never at the first look, and lists drives, fans and alerts."""
    from hddfancontrol_webui.alerts import Alerts

    def local(day: int, hour: int) -> float:
        return time.mktime((2026, 11, day, hour, 0, 0, 0, 0, -1))

    assert Monitor.summary_time("daily", local(17, 10)) == local(17, 9)
    assert Monitor.summary_time("daily", local(17, 8)) == local(16, 9)
    assert Monitor.summary_time("weekly", local(19, 10)) == local(16, 9)  # 16 Nov 2026 is a Monday
    assert Monitor.summary_time("weekly", local(16, 8)) == local(9, 9)

    pwm = "/sys/class/hwmon/hwmon2/pwm1"
    settings = from_dict(
        {
            "drives": [DRIVE],
            "pwms": [{"path": pwm, "start": 200, "stop": 75}],
            "notify_url": "https://x.example/",
            "notify_summary": "daily",
        }
    )
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    monitor.alerts = Alerts(lambda: settings, lambda _: None)
    monitor.alerts.raised = [(local(17, 13), "drive_hot")]
    monitor.history.record(int(local(17, 12)), [("temp", "/dev/disk/by-id/wwn-0x5000c500e0000001", 41.0), ("rpm", pwm, 900.0)])
    monitor.history.record(
        int(local(17, 13)), [("temp", "/dev/disk/by-id/wwn-0x5000c500e0000001", 45.0), ("rpm", pwm, 1100.0)]
    )
    monitor.history.save_smart(DRIVE, {"status": "warning"})
    assert monitor.summary_due(local(17, 10)) is None  # first look: only recorded
    assert monitor.summary_due(local(17, 11)) is None  # same day
    due = monitor.summary_due(local(18, 9) + 1800)
    assert due == local(18, 9)
    text = monitor.build_summary(due)
    assert text.splitlines()[0] == "hddfancontrol: the last 24 hours"
    assert "ST4000VN006: highest 45 °C, average 43 °C" in text
    assert "pwm1: 1000 RPM on average" in text and "Alerts: 1 raised, 0 still on" in text
    assert "SMART: 0 OK, 1 to check, 0 failing" in text
    monitor._settings = lambda: from_dict({**settings.to_dict(), "notify_summary": "off"})
    assert monitor.summary_due(local(19, 10)) is None
    monitor.history.close()


async def test_summary_is_sent_once(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A due summary goes to the alerts once, and is recorded."""
    sent: list[str] = []
    settings = from_dict({"drives": [DRIVE], "notify_url": "https://x.example/", "notify_summary": "weekly"})
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)

    class FakeAlerts:
        raised: list[Any] = []
        active: dict[str, Any] = {}

        def send_summary(self, text: str) -> None:
            sent.append(text)

    monitor.alerts = FakeAlerts()  # type: ignore[assignment]  # only what the summary uses
    monitor.history.set_meta("summary_last_weekly", "0")
    await monitor._send_summary()
    await monitor._send_summary()
    assert len(sent) == 1 and sent[0].startswith("hddfancontrol: the last 7 days")
    monitor.history.close()


async def test_selftests_run_at_night(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """Due self-tests start at night only; one long test per night; a sleeping drive is tried later."""
    other = "/dev/disk/by-id/nvme-Samsung_980_S64"
    calls: list[tuple[str, str]] = []
    asleep = {DRIVE}

    async def selftest(device: str, kind: str) -> dict[str, Any]:
        calls.append((device, kind))
        if device in asleep:
            return {"status": "standby"}
        return (
            {"status": "started"}
            if kind == "short"
            else {"status": "error", "messages": ["unsupported"]} if device == other else {"status": "started"}
        )

    settings = from_dict(
        {"drives": [DRIVE], "monitor_drives": [DRIVE, other], "selftest_short_days": 7, "selftest_long_days": 30}
    )
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    monitor._selftest = selftest
    lines: list[str] = []
    monitor._log = lines.append

    def local(day: int, hour: int) -> float:
        return time.mktime((2026, 11, day, hour, 0, 0, 0, 0, -1))

    assert await monitor.run_selftests(local(17, 12)) == [] and calls == []  # daytime
    # Night: DRIVE sleeps; the other cannot run a long test, so its short one starts.
    assert await monitor.run_selftests(local(18, 3)) == [(other, "short")]
    assert any("cannot start the SMART long self-test" in line for line in lines)
    # DRIVE wakes up: its long test starts, and its short test waits for it.
    asleep.clear()
    calls.clear()
    assert await monitor.run_selftests(local(18, 3) + 60) == [(DRIVE, "long")]
    assert (DRIVE, "short") not in calls
    # Nothing is due again soon.
    calls.clear()
    assert await monitor.run_selftests(local(19, 3)) == [] and calls == []
    # A week later the short tests are due again; DRIVE's long test was 7 days ago, so its short one runs.
    assert sorted(await monitor.run_selftests(local(25, 3))) == sorted([(DRIVE, "short"), (other, "short")])
    monitor.history.close()
