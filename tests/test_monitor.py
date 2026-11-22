"""Recording the history and refreshing the SMART data."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
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


async def test_smart_reading_in_the_background(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """refresh_smart_soon reads once at a time, logs a failure, and stop() ends a reading under way."""
    lines: list[str] = []
    release = asyncio.Event()

    async def reader(device: str) -> dict[str, Any]:
        await release.wait()
        raise RuntimeError("smartctl broken")

    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({"drives": [DRIVE]}), reader)
    monitor._log = lines.append
    assert not monitor.smart_refreshing
    monitor.refresh_smart_soon()
    first = monitor._smart_task
    monitor.refresh_smart_soon()
    assert monitor._smart_task is first and monitor.smart_refreshing
    release.set()
    assert first is not None
    await asyncio.wait_for(first, 5)  # the reading ends, with its error logged
    assert not monitor.smart_refreshing and any("smartctl broken" in line for line in lines)
    release.clear()
    monitor.refresh_smart_soon()
    await asyncio.sleep(0)
    await monitor.stop()
    assert monitor._smart_task is not None and monitor._smart_task.cancelled()
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

    idle = make_monitor(tmp_path / "b", fake_sys, fake_dev, from_dict({"smart_schedule": ""}), reader)
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

    async def until(check: Callable[[], bool]) -> None:
        # Not a fixed pause: a slow CI runner gets up to 5 s.
        for _ in range(500):
            if check():
                return
            await real(0.01)

    monitor.start()
    await until(lambda: any("SMART refresh failed" in line for line in lines) and len(updates) > 2)
    assert all(not task.done() for task in monitor._tasks)
    assert sum("recording failed" in line for line in lines) == 1
    assert sum("SMART refresh failed" in line for line in lines) == 1
    assert len(updates) > 1  # the alerts ran although recording failed
    broken["on"] = False
    await until(lambda: any("recording works again" in line for line in lines))
    assert any("recording works again" in line for line in lines)
    await monitor.stop()
    monitor.history.close()


async def test_smart_schedule_change_applies_at_once(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMART is read at start, then at the runs of smart_schedule; a new expression applies at the next check."""
    real = asyncio.sleep
    start = time.mktime((2026, 11, 17, 0, 0, 30, 0, 0, -1))
    clock = {"now": start}

    async def tick(_: float) -> None:
        clock["now"] += 60
        await real(0.005)

    reads: list[float] = []

    async def reader(device: str) -> dict[str, Any]:
        reads.append(clock["now"])
        return {"status": "ok"}

    monkeypatch.setattr(monitor_module.asyncio, "sleep", tick)
    # Only the monitor's clock: the event loop keeps the real one.
    fake_time = type("FakeTime", (), {"time": staticmethod(lambda: clock["now"])})
    monkeypatch.setattr(monitor_module, "time", fake_time)
    settings = {"value": from_dict({"drives": [DRIVE], "smart_schedule": "@yearly"})}
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings["value"], reader)
    monitor._settings = lambda: settings["value"]
    task = asyncio.create_task(monitor._smart_loop())
    while not reads:
        await real(0.005)
    assert reads == [start + 60]
    settings["value"] = from_dict({"drives": [DRIVE], "smart_schedule": "0 * * * *"})
    while len(reads) < 2:
        await real(0.005)
    task.cancel()
    assert reads[1] == start + 3600  # the first check after 01:00
    monitor.history.close()


def test_summary(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """The summary is due at the runs of its expression, never at the first look, and lists drives, fans and alerts."""
    from hddfancontrol_webui.alerts import Alerts

    def local(day: int, hour: int) -> float:
        return time.mktime((2026, 11, day, hour, 0, 0, 0, 0, -1))

    pwm = "/sys/class/hwmon/hwmon2/pwm1"
    settings = from_dict(
        {
            "drives": [DRIVE],
            "pwms": [{"path": pwm, "start": 200, "stop": 75}],
            "notify_url": "https://x.example/",
            "notify_summary": "0 9 * * *",
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
    assert text.splitlines()[0] == "hddfancontrol: from 17/11 09:00 to 18/11 09:00"
    assert "ST4000VN006: highest 45 °C, average 43 °C" in text
    assert "pwm1: 1000 RPM on average" in text and "Alerts: 1 raised, 0 still on" in text
    assert "SMART: 0 OK, 1 to check, 0 failing" in text
    monitor._settings = lambda: from_dict({**settings.to_dict(), "notify_summary": ""})
    assert monitor.summary_due(local(19, 10)) is None
    # A new expression starts over: only recorded at the first look.
    monitor._settings = lambda: from_dict({**settings.to_dict(), "notify_summary": "0 10 * * *"})
    assert monitor.summary_due(local(19, 11)) is None
    assert monitor.summary_due(local(20, 11)) == local(20, 10)
    monitor.history.close()


async def test_summary_is_sent_once(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A due summary goes to the alerts once, and is recorded."""
    sent: list[str] = []
    settings = from_dict({"drives": [DRIVE], "notify_url": "https://x.example/", "notify_summary": "0 9 * * 1"})
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)

    class FakeAlerts:
        raised: list[Any] = []
        active: dict[str, Any] = {}

        def send_summary(self, text: str) -> None:
            sent.append(text)

    monitor.alerts = FakeAlerts()  # type: ignore[assignment]  # only what the summary uses
    monitor.history.set_meta("summary_last", "0 9 * * 1|0")
    await monitor._send_summary()
    await monitor._send_summary()
    assert len(sent) == 1 and sent[0].startswith("hddfancontrol: from ")
    monitor.history.close()


async def test_selftests_follow_their_schedule(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """Self-tests start at the runs of their expressions, for a few hours; a sleeping drive is tried later."""
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
        # 18 November 2026 is a Wednesday.
        {"drives": [DRIVE], "monitor_drives": [DRIVE, other], "selftest_short": "0 3 * * wed", "selftest_long": "0 3 18 * *"}
    )
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    monitor._selftest = selftest
    lines: list[str] = []
    monitor._log = lines.append

    def local(day: int, hour: int) -> float:
        return time.mktime((2026, 11, day, hour, 0, 0, 0, 0, -1))

    assert await monitor.run_selftests(local(17, 12)) == [] and calls == []  # nothing due
    # Due: DRIVE sleeps; the other cannot run a long test, so its short one starts.
    assert await monitor.run_selftests(local(18, 3)) == [(other, "short")]
    assert any("cannot start the SMART long self-test" in line for line in lines)
    # DRIVE wakes up: its long test starts, and its short test waits for it.
    asleep.clear()
    calls.clear()
    assert await monitor.run_selftests(local(18, 3) + 60) == [(DRIVE, "long")]
    assert (DRIVE, "short") not in calls
    # Past SELFTEST_GRACE nothing is due.
    calls.clear()
    assert await monitor.run_selftests(local(19, 3)) == [] and calls == []
    # The next Wednesday the short tests are due again; DRIVE's long test was 7 days ago, so its short one runs.
    assert sorted(await monitor.run_selftests(local(25, 3))) == sorted([(DRIVE, "short"), (other, "short")])
    monitor.history.close()


def test_fan_wear(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A chosen fan that turns slower at the same PWM than in its first week is reported; the baseline is kept."""
    pwm = "/sys/class/hwmon/hwmon2/pwm1"
    settings = from_dict({"drives": [DRIVE], "pwms": [{"path": pwm, "start": 200, "stop": 75}]})
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    start = 1_000_000
    day = 86400

    def record(first: int, last: int, rpm: float) -> None:
        for ts in range(first, last, 600):
            monitor.history.record(ts, [("pwm", pwm, 50.0), ("rpm", pwm, rpm)])

    record(start, start + 7 * day, 1200.0)
    assert monitor.fan_wear(start + 10 * day) == {}  # history too young for a baseline
    record(start + 30 * day, start + 37 * day, 1180.0)
    assert monitor.fan_wear(start + 37 * day) == {}  # a small drop is noise
    record(start + 37 * day, start + 44 * day, 1000.0)
    worn = monitor.fan_wear(start + 44 * day)
    assert worn[pwm] == pytest.approx(1 - 1000 / 1200, abs=0.01)
    # The baseline stays when the first week is purged.
    monitor.history.purge(10, now=start + 44 * day)
    assert pwm in monitor.fan_wear(start + 44 * day)
    monitor.wear = worn
    found = monitor.conditions({})
    assert found[f"wear:{pwm}"] == ("fan_wear", {"fan": "nct6798 pwm1", "percent": "17"})
    monitor.history.close()


def test_fan_wear_needs_two_weeks_of_history(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """With history_days under WEAR_BASELINE_AGE the purge never lets a fan's history get old enough for a baseline.

    So the check never starts: the settings page says so next to the field.
    """
    from hddfancontrol_webui.monitor import WEAR_BASELINE_AGE

    pwm = "/sys/class/hwmon/hwmon2/pwm1"
    day = 86400
    assert WEAR_BASELINE_AGE == 14 * day  # the hint of the page (settings.js, WEAR_MIN_DAYS) says 14
    for days, starts in ((7, False), (14, True)):
        settings = from_dict({"drives": [DRIVE], "pwms": [{"path": pwm, "start": 200, "stop": 75}], "history_days": days})
        folder = tmp_path / str(days)
        folder.mkdir()
        monitor = make_monitor(folder, fake_sys, fake_dev, settings)
        start = 1_000_000
        for first in range(start, start + 30 * day, day):
            for ts in range(first, first + day, 600):
                monitor.history.record(ts, [("pwm", pwm, 50.0), ("rpm", pwm, 1200.0)])
            monitor.history.purge(days, now=first + day)
            monitor.fan_wear(first + day)
        assert (monitor.history.get_meta(f"fan_baseline_{pwm}") is not None) is starts
        monitor.history.close()


def test_fan_wear_baseline_of_a_fan_added_later(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A fan whose history starts after the rest gets its baseline from its own first week."""
    old, new = "/sys/class/hwmon/hwmon2/pwm1", "/sys/class/hwmon/hwmon2/pwm2"
    settings = from_dict({"drives": [DRIVE], "pwms": [{"path": new, "start": 200, "stop": 75}]})
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    day = 86400
    start = 1_000_000
    monitor.history.record(start, [("rpm", old, 900.0)])  # the history began long before the new fan
    for ts in range(start + 60 * day, start + 67 * day, 600):
        monitor.history.record(ts, [("pwm", new, 50.0), ("rpm", new, 1200.0)])
    assert monitor.fan_wear(start + 70 * day) == {}  # its own history is too young
    assert monitor.history.get_meta(f"fan_baseline_{new}") is None
    for ts in range(start + 74 * day, start + 81 * day, 600):
        monitor.history.record(ts, [("pwm", new, 50.0), ("rpm", new, 900.0)])
    assert monitor.fan_wear(start + 81 * day)[new] == pytest.approx(0.25, abs=0.01)
    assert monitor.history.oldest("rpm", new) == start + 60 * day and monitor.history.oldest() == start
    monitor.history.close()


async def test_a_failing_job_does_not_stop_the_purge(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan wear check or a summary that fails is logged on its own; recording and the purge go on."""
    real = asyncio.sleep

    async def quick(_: float) -> None:
        await real(0.01)

    monkeypatch.setattr(monitor_module.asyncio, "sleep", quick)
    monkeypatch.setattr(monitor_module, "PURGE_PERIOD", 0.0)
    lines: list[str] = []
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({"drives": [DRIVE]}))
    monitor._log = lines.append
    purges: list[int] = []

    def broken_wear(*_: Any) -> dict[str, float]:
        raise ValueError("bad baseline")

    async def broken_summary() -> None:
        raise ValueError("bad summary")

    monkeypatch.setattr(monitor, "fan_wear", broken_wear)
    monkeypatch.setattr(monitor, "_send_summary", broken_summary)
    monkeypatch.setattr(monitor.history, "purge", lambda days, now=None: purges.append(days))
    monitor.start()
    await real(0.1)
    await monitor.stop()
    assert len(purges) > 1
    assert sum("fan wear check failed" in line for line in lines) == 1
    assert sum("summary failed" in line for line in lines) == 1
    assert not any("recording failed" in line for line in lines)
    monitor.history.close()


async def test_smart_readings_feed_the_trend(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A fresh reading keeps its counters in the trend; a drive asleep or unreadable adds nothing."""
    answers = [
        {"status": "warning", "pending_sectors": 3, "power_on_hours": 900, "model": "X", "percentage_used": None},
        {"status": "standby"},
        {"status": "error", "messages": ["x"]},
    ]

    async def reader(device: str) -> dict[str, Any]:
        return answers.pop(0)

    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({"drives": [DRIVE]}), reader)
    for _ in range(3):
        await monitor.refresh_smart()
    trend = monitor.history.smart_history(DRIVE)
    assert set(trend) == {"pending_sectors", "power_on_hours"}
    assert [value for _, value in trend["pending_sectors"]] == [3.0]
    monitor.history.close()


def test_what_the_monitor_takes_from_the_hardware(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """Fan readings, fan names, stable drive names, remembered ids and drive names in alerts, on several chip shapes."""
    linked = fake_sys / "devices" / "platform" / "it87.2608" / "hwmon" / "hwmon3"
    linked.mkdir(parents=True)
    for name, value in {
        "pwm1": "90",
        "pwm1_enable": "2",
        "fan1_input": "1500",
        "fan2_input": "700",
        "temp3_input": "-5000",
    }.items():
        (linked / name).write_text(value + "\n")
    (fake_sys / "class" / "hwmon" / "hwmon3").symlink_to(linked)
    nvme = "/dev/disk/by-id/nvme-Samsung_980_S64"
    settings = from_dict({"monitor_drives": [DRIVE, nvme]})
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, settings)
    monitor._labels = lambda: {"/sys/class/hwmon/hwmon2/pwm2": "Rear", nvme: "Boot"}
    assert monitor.record_once() == 1 + 5
    assert monitor._fans == {
        "/sys/class/hwmon/hwmon2/pwm1": (128 * 100 / 255, 812),
        "/sys/class/hwmon/hwmon2/pwm2": (100.0, None),
        "/sys/class/hwmon/hwmon3/pwm1": (90 * 100 / 255, 1500),
    }
    series = {(s["kind"], s["name"]): s["points"][-1][1] for s in monitor.history.query(24 * 365)["series"]}
    assert series == {
        ("temp", "/dev/disk/by-id/wwn-0x5000c500e0000001"): 40.0,
        ("pwm", "/sys/class/hwmon/hwmon2/pwm1"): 50.2,
        ("rpm", "/sys/class/hwmon/hwmon2/pwm1"): 812.0,
        ("pwm", "/sys/class/hwmon/hwmon2/pwm2"): 100.0,
        ("pwm", "/sys/class/hwmon/hwmon3/pwm1"): 35.3,
        ("rpm", "/sys/class/hwmon/hwmon3/pwm1"): 1500.0,
    }
    assert [monitor._fan_name(path) for path in sorted(monitor._fans)] == ["nct6798 pwm1", "Rear", "hwmon3 pwm1"]
    assert monitor._fan_name("/sys/class/hwmon/hwmon9/pwm4") == "pwm4"
    assert monitor._stable_names() == {"sda": "/dev/disk/by-id/wwn-0x5000c500e0000001", "nvme0n1": nvme}
    monitor.remember_ids()
    assert monitor.history.drive_ids(DRIVE) == sorted({DRIVE, "/dev/disk/by-id/wwn-0x5000c500e0000001"})
    assert monitor.history.drive_ids(nvme) == sorted({nvme, "/dev/disk/by-id/nvme-eui.002538"})
    monitor.history.save_smart(DRIVE, {"status": "error", "messages": ["no"]})
    monitor.history.save_smart(nvme, {"status": "error", "messages": ["no"]})
    found = monitor.conditions({})
    assert found[f"smart_error:{DRIVE}"][1]["drive"] == "ST4000VN006 (sda)"
    assert found[f"smart_error:{nvme}"][1]["drive"] == "Boot"
    assert "missing" not in json.dumps(list(monitor.smart_view().values()))
    monitor.history.close()


def test_tidy_deletes_nothing_while_the_saved_settings_are_broken(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """With settings.json broken, the environment stands in: tidy keeps every sample and SMART reading, and says so once."""
    lines: list[str] = []
    broken = [True]
    history = History(tmp_path / "history.db")
    monitor = Monitor(
        history,
        TempLog(tmp_path / "temps.jsonl"),
        Hardware(fake_dev, fake_sys),
        lambda: Settings(history_days=1),  # the environment: one day, no monitored drive
        log=lines.append,
        settings_ok=lambda: not broken[0],
    )
    old = int(time.time()) - 10 * 86400
    history.record(old, [("drive", DRIVE, 40.0)])
    history.save_smart(DRIVE, {"status": "ok"})
    history.record_smart(DRIVE, {"pending_sectors": 1.0}, ts=old)
    monitor.tidy()
    monitor.tidy()
    assert list(history.smart()) == [DRIVE]
    assert history.smart_history(DRIVE) != {}
    assert history.query(24 * 30)["series"] != []
    assert len(lines) == 1 and "kept until they are fixed" in lines[0]
    # Fixed: the cleaning runs again.
    broken[0] = False
    monitor.tidy()
    assert history.smart() == {} and history.query(24 * 30)["series"] == []
    history.close()
