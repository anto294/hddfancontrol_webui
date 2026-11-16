"""Alerts: when they start and end, what is logged and what is sent where."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from hddfancontrol_webui.alerts import Alerts, render
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.history import History
from hddfancontrol_webui.monitor import FAN_STALL_SECONDS, Monitor
from hddfancontrol_webui.notify import NotifyError, send
from hddfancontrol_webui.settings import Settings, from_dict

from .test_monitor import DRIVE, make_monitor

PWM1 = "/sys/class/hwmon/hwmon2/pwm1"


@pytest.fixture
async def receiver(aiohttp_server: Any) -> tuple[TestServer, list[dict[str, Any]]]:
    """A server that keeps what it is sent; /fail answers 500."""
    received: list[dict[str, Any]] = []

    async def take(request: web.Request) -> web.Response:
        received.append({"headers": dict(request.headers), "body": await request.text()})
        return web.Response(status=500 if request.path == "/fail" else 200)

    app = web.Application()
    app.router.add_post("/{tail:.*}", take)
    return await aiohttp_server(app), received


async def test_send_formats(receiver: tuple[TestServer, list[dict[str, Any]]]) -> None:
    """ntfy gets the text and headers, Gotify and JSON get a JSON body."""
    server, received = receiver
    await send(str(server.make_url("/topic")), "ntfy", "Caldo è", True)
    assert received[0]["body"] == "Caldo è"
    assert received[0]["headers"]["Priority"] == "high" and received[0]["headers"]["Title"] == "hddfancontrol"
    await send(str(server.make_url("/message")), "gotify", "ok", False)
    assert json.loads(received[1]["body"]) == {"title": "hddfancontrol", "message": "ok", "priority": 4}
    await send(str(server.make_url("/hook")), "json", "x", True)
    assert json.loads(received[2]["body"])["level"] == "alert"
    with pytest.raises(NotifyError, match="HTTP 500"):
        await send(str(server.make_url("/fail")), "ntfy", "x", True)
    with pytest.raises(NotifyError):
        await send("http://127.0.0.1:1/", "ntfy", "x", True)


async def test_alerts_start_and_end() -> None:
    """A new condition is logged and sent once; its end is logged and sent too."""
    sent: list[tuple[str, str, str, bool]] = []
    logs: list[str] = []

    async def sender(url: str, fmt: str, text: str, urgent: bool) -> None:
        sent.append((url, fmt, text, urgent))

    settings = Settings(notify_url="https://ntfy.sh/x", notify_language="it")
    alerts = Alerts(lambda: settings, logs.append, sender)
    hot = {"hot:sda": ("drive_hot", {"drive": "sda", "temp": "51", "max": "50"})}
    await alerts.update(hot)
    await alerts.update(hot)
    await alerts.flush()
    assert len(sent) == 1 and sent[0][3] is True and "51 °C" in sent[0][2] and "massima" in sent[0][2]
    assert logs == ["ALERT: sda is at 51 °C, at or above the maximum of 50 °C."]
    assert alerts.status()[0]["kind"] == "drive_hot"
    await alerts.update({})
    await alerts.flush()
    assert sent[1][3] is False and sent[1][2].startswith("Tornato alla normalità")
    assert logs[1].startswith("Over: ") and alerts.status() == []
    await alerts.test()
    assert "prova" in sent[2][2]


async def test_alerts_without_address_or_with_a_failing_one() -> None:
    """No address: only logged. A failing service is logged, never raised."""
    logs: list[str] = []

    async def failing(url: str, fmt: str, text: str, urgent: bool) -> None:
        raise NotifyError("down")

    settings = Settings()
    alerts = Alerts(lambda: settings, logs.append, failing)
    await alerts.update({"safety": ("safety", {})})
    assert logs == ["ALERT: No new drive temperature: every fan runs at full speed."]
    with pytest.raises(NotifyError):
        await alerts.test()
    settings = Settings(notify_url="https://x.example/")
    await alerts.update({})
    await alerts.flush()
    assert logs[-1] == "WARNING: cannot send the notification: down"
    await alerts.stop()


async def test_slow_notifications_do_not_hold_the_monitor_up() -> None:
    """update() returns at once; messages go out in order; a full queue drops and logs."""
    import asyncio

    from hddfancontrol_webui import alerts as alerts_module

    gate = asyncio.Event()
    sent: list[str] = []
    logs: list[str] = []

    async def slow(url: str, fmt: str, text: str, urgent: bool) -> None:
        await gate.wait()
        if "boom" in text:
            raise ValueError("odd")
        sent.append(text)

    settings = Settings(notify_url="https://x.example/")
    alerts = Alerts(lambda: settings, logs.append, slow)
    conditions = {f"hot:sd{c}": ("drive_hot", {"drive": f"sd{c}", "temp": "51", "max": "50"}) for c in "abc"}
    await asyncio.wait_for(alerts.update(conditions), 1)
    assert sent == []
    await alerts.update({"hot:boom": ("drive_hot", {"drive": "boom", "temp": "51", "max": "50"})})
    gate.set()
    await alerts.flush()
    # The three starts, then (boom failing in between) the three ends.
    assert [text.split()[0] for text in sent] == ["sda", "sdb", "sdc", "Back", "Back", "Back"]
    assert any("ValueError" in line for line in logs)
    # A queue that is full while the server hangs.
    alerts._queue = asyncio.Queue(1)
    await alerts.update({"x": ("safety", {}), "y": ("safety", {}), "z": ("safety", {})})
    assert any("dropped" in line for line in logs)
    await alerts.stop()
    assert alerts_module.QUEUE_SIZE == 100


def test_render_falls_back_to_english() -> None:
    """A text missing in a language is the English one; an unknown key is itself."""
    assert render("it", "alert.safety", {}).startswith("Nessuna")
    assert render("en", "alert.nothing", {}) == "alert.nothing"


def conditions_monitor(tmp_path: Path, fake_sys: Path, fake_dev: Path, **settings: Any) -> Monitor:
    """A monitor whose sda sample is fresh, with alerts that only log."""
    doc = {"drives": [DRIVE], "monitor_drives": [DRIVE], "pwms": [{"path": PWM1, "start": 200, "stop": 60}]}
    monitor = make_monitor(tmp_path, fake_sys, fake_dev, from_dict({**doc, **settings}))
    monitor.alerts = Alerts(monitor._settings, lambda _: None)
    monitor._labels = lambda: {PWM1: "Front fan"}
    return monitor


def test_conditions_hot_drive(tmp_path: Path, fake_sys: Path, fake_dev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh sample at the maximum raises drive_hot; it ends only under the maximum minus the margin."""
    monitor = conditions_monitor(tmp_path, fake_sys, fake_dev, drive_temp_min=30, drive_temp_max=40, target_temp=35)
    monkeypatch.setattr("hddfancontrol_webui.monitor.FRESH_SAMPLE_SECONDS", 10**9)
    found = monitor.conditions({})
    assert found["hot:sda"] == ("drive_hot", {"drive": "sda", "temp": "40", "max": "40"})
    assert monitor.alerts is not None
    monitor.alerts.active["hot:sda"] = {"kind": "drive_hot", "params": {}, "since": 0}
    monitor._settings = lambda: from_dict({"drives": [DRIVE], "drive_temp_min": 30, "drive_temp_max": 41, "target_temp": 35})
    assert "hot:sda" in monitor.conditions({})  # 40 > 41 - 2: still on
    monkeypatch.setattr("hddfancontrol_webui.monitor.FRESH_SAMPLE_SECONDS", 1)
    assert "hot:sda" not in monitor.conditions({})  # an old sample says nothing
    monitor.history.close()


def test_conditions_stalled_fan(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A fan seen turning that shows 0 RPM well over its stop value is stalled after a while."""
    monitor = conditions_monitor(tmp_path, fake_sys, fake_dev)
    monitor.record_once()
    assert not monitor.conditions({"mode": "running"}, now=0)  # seen turning at 812 RPM
    (fake_sys / "class/hwmon/hwmon2/fan1_input").write_text("0\n")
    (fake_sys / "class/hwmon/hwmon2/pwm1").write_text("200\n")
    monitor.record_once()
    assert "stalled:" + PWM1 not in monitor.conditions({"mode": "running"}, now=10)
    found = monitor.conditions({"mode": "running"}, now=10 + FAN_STALL_SECONDS)
    assert found["stalled:" + PWM1] == ("fan_stalled", {"fan": "Front fan", "percent": "78"})
    assert not monitor.conditions({"mode": "pwm_test"}, now=1000)  # the PWM test stops fans on purpose
    (fake_sys / "class/hwmon/hwmon2/pwm1").write_text("100\n")  # under halfway between stop and start
    monitor.record_once()
    assert not monitor.conditions({"mode": "running"}, now=2000)
    monitor.history.close()


def test_conditions_safety_smart_missing(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """Fan safety, bad SMART and lost fan chips are alerts too."""
    monitor = conditions_monitor(tmp_path, fake_sys, fake_dev)
    monitor.history.save_smart(DRIVE, {"status": "failing"})
    monitor.history.save_smart("/dev/disk/by-id/ata-Old", {"status": "warning"})
    status = {"fans": {"safety": True, "auto": {}}, "missing_pwms": [PWM1, "/sys/class/hwmon/hwmon9/pwm3"]}
    found = monitor.conditions(status)
    assert found["safety"] == ("safety", {})
    assert found["smart:" + DRIVE] == (
        "smart_failing",
        {"drive": "ST4000VN006 (sda)", "warnings": [], "counts": {"failed": []}},
    )
    assert "smart:/dev/disk/by-id/ata-Old" not in found  # not monitored
    assert found["fans_missing"] == ("fans_missing", {"fans": "Front fan, pwm3"})
    # A control loop that failed runs the fans at full speed: that is an alert too.
    assert "safety" in monitor.conditions({"fans": {"error": "boom", "auto": {}}})
    monitor._labels = lambda: {}
    assert monitor._fan_name(PWM1) == "nct6798 pwm1"
    monitor.history.close()


def test_conditions_missing_drive_and_unreadable_smart(tmp_path: Path, fake_sys: Path, fake_dev: Path) -> None:
    """A monitored drive gone from the host, or whose SMART data cannot be read, raises an alert."""
    gone = "/dev/disk/by-id/ata-Gone_123"
    nvme = "/dev/disk/by-id/nvme-Samsung_980_S64"
    monitor = conditions_monitor(tmp_path, fake_sys, fake_dev)
    monitor._settings = lambda: from_dict({"drives": [DRIVE], "monitor_drives": [DRIVE, gone, nvme, "/dev/sda"]})
    monitor.history.save_smart(gone, {"status": "error", "messages": ["No such device"]})
    monitor.history.save_smart(nvme, {"status": "error", "messages": ["Smartctl open device failed"]})
    found = monitor.conditions({})
    assert found["missing:" + gone] == ("drive_missing", {"drive": "ata-Gone_123"})
    assert "smart_error:" + gone not in found  # missing says it all
    assert found["smart_error:" + nvme] == (
        "smart_error",
        {"drive": "Samsung 980 (nvme0n1)", "error": "Smartctl open device failed"},
    )
    assert "missing:/dev/sda" not in found and "missing:" + DRIVE not in found
    # Without any drive in /dev, nothing is said about missing drives.
    monitor.hardware = Hardware(tmp_path / "nothing", fake_sys)
    assert not any(key.startswith("missing:") for key in monitor.conditions({}))
    monitor.history.close()


async def test_notes_on_alerts(tmp_path: Path) -> None:
    """A note hides an alert until it gets worse or ends; it survives a restart in the database."""
    settings = Settings()
    store = History(tmp_path / "history.db")
    alerts = Alerts(lambda: settings, lambda _: None, store=store)
    smart = {"drive": "sda", "warnings": ["crc_errors"], "counts": {"crc_errors": 5}}
    await alerts.update(
        {"smart:a": ("smart_warning", smart), "hot:sda": ("drive_hot", {"drive": "sda", "temp": "51", "max": "50"})}
    )
    assert alerts.ack("smart:a") and alerts.ack("hot:sda") and alerts.ack("banner:no_auth")
    assert not alerts.ack("nothing")
    await alerts.update(
        {"smart:a": ("smart_warning", smart), "hot:sda": ("drive_hot", {"drive": "sda", "temp": "52", "max": "50"})}
    )
    assert all(a["acknowledged"] for a in alerts.status())  # a new temperature is the same alert

    again = Alerts(lambda: settings, lambda _: None, store=store)
    assert set(again.acks) == {"smart:a", "hot:sda", "banner:no_auth"}
    worse = {**smart, "counts": {"crc_errors": 6}}
    await again.update({"smart:a": ("smart_warning", worse)})
    assert again.status()[0]["acknowledged"] is False  # one more CRC error: shown again
    assert "hot:sda" not in again.acks and "banner:no_auth" in again.acks  # the hot alert ended
    again.unack("banner:no_auth")
    again.unack("banner:no_auth")
    assert set(store.notes()) == {"smart:a"}
    store.close()


def test_render_smart_problems() -> None:
    """SMART warning codes become the sentences of the web UI."""
    text = render("it", "alert.smart_warning", {"drive": "sda", "warnings": ["crc_errors"]})
    assert text == "sda: Errori CRC dell'interfaccia: controlla il cavo. (SMART)"
