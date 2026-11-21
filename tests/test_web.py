"""The core API: settings, fans, history, SMART; the peer check and the audit log."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from conftest import ScriptMaker

from hddfancontrol_webui.alerts import Alerts
from hddfancontrol_webui.fancontrol import FanController, PwmWriter
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.history import History
from hddfancontrol_webui.labels import Labels
from hddfancontrol_webui.monitor import Monitor
from hddfancontrol_webui.notify import NotifyError
from hddfancontrol_webui.sensors import SensorReport
from hddfancontrol_webui.settings import NOTIFY_MASK, SettingsStore
from hddfancontrol_webui.supervisor import LogBuffer, Supervisor
from hddfancontrol_webui.templog import TempLog
from hddfancontrol_webui.web import HARDWARE, LABELS, MONITOR, SUPERVISOR, TEMP_LOG, create_app, mask_addresses, mask_drives

PWM1 = "/sys/class/hwmon/hwmon2/pwm1"
DRIVE = "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
FETCH = {"X-Requested-With": "fetch"}

Client = TestClient[Any, Any]
ClientMaker = Callable[..., Awaitable[Client]]


async def settled(client: Client) -> dict[str, Any]:
    """Wait until no SMART reading and no sensor scan runs in the background; return the status."""
    for _ in range(250):
        status: dict[str, Any] = await (await client.get("/api/status")).json()
        if not status["smart_refreshing"] and status["sensors"]["state"] != "running":
            return status
        await asyncio.sleep(0.02)
    raise AssertionError("still running")


@pytest.fixture
def make_client(aiohttp_client: Any, tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker) -> ClientMaker:
    """Build a client on an app with fake hardware and programs."""

    async def make(
        peers: frozenset[int] | None = None,
        load_modules: bool = True,
        sensor_scan: Any = None,
        smart_reader: Any = None,
        env: dict[str, str] | None = None,
    ) -> Client:
        config = tmp_path / "config"
        logs = LogBuffer(echo=False)
        log_path = config / "templog" / "temps.jsonl"
        log_path.parent.mkdir(parents=True)
        log_path.write_text(
            json.dumps({"time_utc": "2026-11-16T10:00:00Z", "measures": [{"device": "sda", "temp_celcius": 41.0}]}) + "\n"
        )
        temp_log = TempLog(log_path)
        fans = FanController(PwmWriter(fake_sys), temp_log.latest, lambda _: None)
        supervisor = Supervisor(
            SettingsStore(config, env or {}),
            logs,
            log_path,
            fans,
            hddfancontrol=make_script("hddfancontrol", "time.sleep(60)"),
            hdidle=make_script("hd-idle", "time.sleep(60)"),
            hardware=Hardware(fake_dev, fake_sys),
        )
        hardware = supervisor.hardware
        assert hardware is not None
        history = History(config / "history.db")

        async def read(device: str) -> dict[str, Any]:
            return {"status": "ok", "model": "M"}

        monitor = Monitor(history, temp_log, hardware, lambda: supervisor.settings, smart_reader or read)

        async def scan() -> SensorReport:
            return SensorReport(state="done")

        app = create_app(
            supervisor,
            hardware,
            temp_log,
            history,
            monitor,
            Labels(config),
            sensor_scan or scan,
            peers=peers,
            load_modules=load_modules,
        )

        async def stop(app: web.Application) -> None:
            await supervisor.stop()
            history.close()

        app.on_cleanup.append(stop)
        client: Client = await aiohttp_client(app)
        return client

    return make


async def test_status_and_health(make_client: ClientMaker) -> None:
    """The core answers the API and its health check; the status has no login state."""
    client = await make_client()
    assert (await client.get("/")).status == 404  # pages are the gateway's
    response = await client.get("/healthz")
    assert response.status == 200
    status = await (await client.get("/api/status")).json()
    assert "auth" not in status
    assert status["latest"]["temps"] == {"sda": 41.0}
    assert status["mode"] == "waiting"
    assert status["checks"] == []  # filled at start by __main__ (checks.py)


async def test_settings_api(make_client: ClientMaker) -> None:
    """Settings are checked, saved and applied, and can be reset."""
    client = await make_client()
    data = await (await client.get("/api/settings")).json()
    assert data["source"] == "environment"

    bad = await client.put("/api/settings", json={"drives": ["/etc/passwd"]}, headers=FETCH)
    assert bad.status == 400
    assert (await bad.json())["problems"][0] == {"field": "drives.0", "code": "invalid_drive", "params": {}}
    broken = await client.put("/api/settings", data="{", headers={**FETCH, "Content-Type": "application/json"})
    assert broken.status == 400 and (await broken.json())["error"] == "invalid_json"
    deep = await client.put("/api/settings", data="[" * 60000, headers={**FETCH, "Content-Type": "application/json"})
    assert deep.status == 400 and (await deep.json())["error"] == "invalid_json"  # not a 500 with a traceback
    huge = await client.put("/api/settings", data="9" * 60000, headers={**FETCH, "Content-Type": "application/json"})
    assert huge.status == 400 and (await huge.json())["error"] == "invalid_json"

    ghost = {"drives": [DRIVE], "pwms": [{"path": "/sys/class/hwmon/hwmon9/pwm1", "start": 200, "stop": 75}]}
    refused = await client.put("/api/settings", json=ghost, headers=FETCH)
    assert refused.status == 400 and (await refused.json())["problems"][0]["code"] == "unknown_pwm"

    doc = {"drives": [DRIVE], "pwms": [{"path": PWM1, "start": 200, "stop": 75}]}
    saved = await client.put("/api/settings", json=doc, headers=FETCH)
    assert saved.status == 200 and (await saved.json())["source"] == "file"
    status = await (await client.get("/api/status")).json()
    assert status["mode"] == "running"

    reset = await client.delete("/api/settings", headers=FETCH)
    assert (await reset.json())["source"] == "environment"


async def test_manual_api(make_client: ClientMaker, fake_sys: Path) -> None:
    """Manual control accepts any PWM file of the host, and only valid values."""
    client = await make_client()
    fans = [{"path": PWM1, "start": 200, "stop": 75}, {"path": "/sys/class/hwmon/hwmon2/pwm2", "start": 200, "stop": 75}]
    response = await client.put("/api/settings", json={"drives": [DRIVE], "pwms": fans[:1]}, headers=FETCH)
    assert response.status == 200
    response = await client.put("/api/manual/pwm", json={"path": PWM1, "value": 10}, headers=FETCH)
    assert response.status == 409
    response = await client.put("/api/manual", json={"enabled": "yes"}, headers=FETCH)
    assert response.status == 400
    response = await client.put("/api/manual", json={"enabled": True}, headers=FETCH)
    assert response.status == 200
    response = await client.put("/api/manual/pwm", json={"path": "/sys/class/hwmon/hwmon2/name", "value": 1}, headers=FETCH)
    assert response.status == 400
    response = await client.put("/api/manual/pwm", json={"path": PWM1, "value": 256}, headers=FETCH)
    assert response.status == 400
    response = await client.put("/api/manual/pwm", json={"path": PWM1, "value": True}, headers=FETCH)
    assert response.status == 400
    response = await client.put("/api/manual/pwm", json={"path": PWM1, "value": 77}, headers=FETCH)
    assert response.status == 200
    assert (fake_sys / "class/hwmon/hwmon2/pwm1").read_text().strip() == "77"
    other = await client.put("/api/manual/pwm", json={"path": "/sys/class/hwmon/hwmon2/pwm2", "value": 1}, headers=FETCH)
    assert other.status == 200  # any output of the host, to find out which fan it drives
    response = await client.put("/api/settings", json={"drives": [DRIVE], "pwms": fans}, headers=FETCH)
    assert response.status == 200
    response = await client.put("/api/manual", json={"enabled": True}, headers=FETCH)
    assert response.status == 200
    (fake_sys / "class/hwmon/hwmon2/pwm2").chmod(0o400)
    denied = await client.put("/api/manual/pwm", json={"path": "/sys/class/hwmon/hwmon2/pwm2", "value": 1}, headers=FETCH)
    assert denied.status in (200, 500)
    response = await client.put("/api/manual", json={"enabled": False}, headers=FETCH)
    assert response.status == 200


async def test_pwm_test_api(make_client: ClientMaker) -> None:
    """Only PWM files of the host can be tested, and a second test is refused."""
    client = await make_client()
    response = await client.post("/api/pwm-test", json={"paths": []}, headers=FETCH)
    assert response.status == 400
    unknown = await client.post("/api/pwm-test", json={"paths": ["/sys/power/state"]}, headers=FETCH)
    assert (await unknown.json())["error"] == "unknown_pwm"
    response = await client.post("/api/pwm-test", json={"paths": [PWM1]}, headers=FETCH)
    assert response.status == 200
    response = await client.post("/api/pwm-test", json={"paths": [PWM1]}, headers=FETCH)
    assert response.status == 409
    response = await client.put("/api/settings", json={}, headers=FETCH)
    assert response.status == 409
    response = await client.delete("/api/settings", headers=FETCH)
    assert response.status == 409
    response = await client.put("/api/manual", json={"enabled": True}, headers=FETCH)
    assert response.status == 409
    response = await client.post("/api/restart", headers=FETCH)
    assert response.status == 409
    response = await client.delete("/api/pwm-test", headers=FETCH)
    assert response.status == 200


async def test_read_endpoints(make_client: ClientMaker) -> None:
    """Hardware, history, SMART, logs and the sensor scan."""
    client = await make_client()
    hardware = await (await client.get("/api/hardware")).json()
    assert hardware["hwmons"][0]["pwms"][0]["path"] == PWM1
    response = await client.get("/api/history?hours=x")
    assert response.status == 400
    response = await client.get("/api/history?hours=0")
    assert response.status == 400
    history = await (await client.get("/api/history?hours=24")).json()
    assert "series" in history
    smart = await (await client.get("/api/smart")).json()
    assert smart == {}
    assert (await client.post("/api/smart", headers=FETCH)).status == 202
    await settled(client)
    assert await (await client.get("/api/smart")).json() == {}
    nvme = "/dev/disk/by-id/nvme-Samsung_980_S64"
    response = await client.put("/api/settings", json={"monitor_drives": [nvme]}, headers=FETCH)
    assert response.status == 200
    assert (await client.post("/api/smart", headers=FETCH)).status == 202
    await settled(client)
    smart = await (await client.get("/api/smart")).json()
    assert list(smart) == [nvme] and smart[nvme]["model"] == "M"
    response = await client.get("/api/logs?after=x")
    assert response.status == 400
    logs = await (await client.get("/api/logs?after=0")).json()
    assert "lines" in logs
    scan = await client.post("/api/sensors/scan", headers=FETCH)
    assert scan.status == 202 and (await scan.json())["state"] == "running"
    assert (await settled(client))["sensors"]["state"] == "done"


async def test_no_sensor_scan_when_loading_modules_is_off(make_client: ClientMaker) -> None:
    """With LOAD_SENSOR_MODULES=false the API never runs sensors-detect or modprobe: 409 scan_disabled."""
    client = await make_client(load_modules=False)
    scan = await client.post("/api/sensors/scan", headers=FETCH)
    assert scan.status == 409 and (await scan.json())["error"] == "scan_disabled"


async def test_a_failed_sensor_scan_never_stays_running(make_client: ClientMaker) -> None:
    """A scan that raises ends as done with its error: the next scan is not refused as busy."""

    async def broken() -> SensorReport:
        raise RuntimeError("scan broken")

    client = await make_client(sensor_scan=broken)
    scan = await client.post("/api/sensors/scan", headers=FETCH)
    assert scan.status == 202
    data = (await settled(client))["sensors"]
    assert data["state"] == "done" and "scan broken" in data["detect_error"]
    again = await client.post("/api/sensors/scan", headers=FETCH)
    assert again.status == 202
    await settled(client)


async def test_a_sensor_scan_looks_for_missing_fan_chips(make_client: ClientMaker) -> None:
    """The scan may load the module of a missing fan chip: the supervisor looks for it at once, even after a failed scan."""

    async def broken() -> SensorReport:
        raise RuntimeError("scan broken")

    client = await make_client(sensor_scan=broken)
    looks: list[bool] = []

    async def recheck() -> bool:
        looks.append(True)
        raise RuntimeError("look broken")  # logged, never left running

    client.app[SUPERVISOR].recheck_fans = recheck  # type: ignore[method-assign]  # a stand-in that records the call
    assert (await client.post("/api/sensors/scan", headers=FETCH)).status == 202
    await settled(client)
    for _ in range(100):
        if looks:
            break
        await asyncio.sleep(0.02)
    assert looks
    assert any("look broken" in line["text"] for line in client.app[SUPERVISOR].logs.since(0))


async def test_slow_smart_reads_and_sensor_scans_answer_at_once(make_client: ClientMaker) -> None:
    """Both run in the background: one drive can take SMARTCTL_TIMEOUT, a scan RUN_TIMEOUT, more than the gateway waits.

    The page follows them through the status it asks for anyway
    (smart_refreshing, sensors.state) and reads the SMART data once over.
    """
    release = asyncio.Event()

    async def slow_reader(device: str) -> dict[str, Any]:
        await release.wait()
        return {"status": "ok", "model": "M"}

    async def slow_scan() -> SensorReport:
        await release.wait()
        return SensorReport(state="done")

    client = await make_client(sensor_scan=slow_scan, smart_reader=slow_reader)
    nvme = "/dev/disk/by-id/nvme-Samsung_980_S64"
    response = await client.put("/api/settings", json={"monitor_drives": [nvme]}, headers=FETCH)
    assert response.status == 200
    smart = await asyncio.wait_for(client.post("/api/smart", headers=FETCH), 2)
    assert smart.status == 202
    again = await asyncio.wait_for(client.post("/api/smart", headers=FETCH), 2)
    assert again.status == 202  # the same reading goes on
    assert (await (await client.get("/api/status")).json())["smart_refreshing"] is True
    scan = await asyncio.wait_for(client.post("/api/sensors/scan", headers=FETCH), 2)
    assert scan.status == 202 and (await scan.json())["state"] == "running"
    busy = await client.post("/api/sensors/scan", headers=FETCH)
    assert busy.status == 409
    release.set()
    await settled(client)
    assert list(await (await client.get("/api/smart")).json()) == [nvme]


async def test_labels_api(make_client: ClientMaker) -> None:
    """Labels are set without restarting anything, and checked."""
    client = await make_client()
    labels = await (await client.get("/api/labels")).json()
    assert labels == {}
    saved = await client.put("/api/labels", json={"key": PWM1, "label": "Front fan"}, headers=FETCH)
    assert await saved.json() == {PWM1: "Front fan"}
    bad = await client.put("/api/labels", json={"key": "/etc/x", "label": "a"}, headers=FETCH)
    assert (await bad.json())["error"] == "invalid_label_key"
    response = await client.put("/api/labels", json={"key": PWM1}, headers=FETCH)
    assert response.status == 400


def test_static_files_need_no_inline_style() -> None:
    """The pages and scripts set no style attribute, which the CSP would block."""
    static = Path(__file__).parent.parent / "hddfancontrol_webui" / "static"
    pattern = re.compile(r"""\bstyle\s*[:=]|setAttribute\(\s*["']style|<style""")
    for path in [*static.glob("*.js"), *static.glob("*.html")]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name


async def test_read_only_api(make_client: ClientMaker) -> None:
    """With /sys read only, the status says so and nothing that writes the fans starts."""
    client = await make_client()
    client.server.app[SUPERVISOR].read_only = True
    status = await (await client.get("/api/status")).json()
    assert status["read_only"] is True and status["mode"] == "read_only"
    manual = await client.put("/api/manual", json={"enabled": True}, headers=FETCH)
    assert manual.status == 409 and (await manual.json())["error"] == "read_only"
    test = await client.post("/api/pwm-test", json={"paths": [PWM1]}, headers=FETCH)
    assert test.status == 409 and (await test.json())["error"] == "read_only"


async def test_notify_url_never_leaves_the_core_in_clear(make_client: ClientMaker) -> None:
    """Every answer with the settings masks notify_url; the masked value sent back keeps the saved address."""
    secret = "https://gotify.lan/message?token=AbC123"
    client = await make_client(env={"NOTIFY_URL": "https://ntfy.sh/env-topic-42"})
    data = await (await client.get("/api/settings")).json()
    assert data["source"] == "environment" and data["settings"]["notify_url"] == f"https://ntfy.sh/{NOTIFY_MASK}"
    saved = await client.put("/api/settings", json={"notify_url": secret, "notify_format": "gotify"}, headers=FETCH)
    assert saved.status == 200 and "AbC123" not in await saved.text()
    supervisor = client.app[SUPERVISOR]
    assert supervisor.settings.notify_url == secret
    # The page saves without touching the field: the document it got goes back as it is.
    shown = (await (await client.get("/api/settings")).json())["settings"]
    assert shown["notify_url"] == f"https://gotify.lan/message?token={NOTIFY_MASK}"
    again = await client.put("/api/settings", json={**shown, "notify_repeat_h": 2}, headers=FETCH)
    assert again.status == 200 and supervisor.settings.notify_url == secret and supervisor.settings.notify_repeat_h == 2
    edited = await client.put(
        "/api/settings", json={**shown, "notify_url": shown["notify_url"].replace("gotify", "evil")}, headers=FETCH
    )
    assert edited.status == 400 and (await edited.json())["problems"][0]["code"] == "notify_url_masked"
    assert supervisor.settings.notify_url == secret
    cleared = await client.put("/api/settings", json={**shown, "notify_url": ""}, headers=FETCH)
    assert cleared.status == 200 and supervisor.settings.notify_url == ""
    reset = await client.delete("/api/settings", headers=FETCH)
    assert (await reset.json())["settings"]["notify_url"] == f"https://ntfy.sh/{NOTIFY_MASK}"


async def test_notify_test_api(make_client: ClientMaker) -> None:
    """The test message needs a saved address, and says when it cannot be sent."""
    client = await make_client()
    sent: list[str] = []
    fail = False

    async def sender(url: str, fmt: str, text: str, urgent: bool) -> None:
        if fail:
            raise NotifyError("down")
        sent.append(url)

    supervisor = client.app[SUPERVISOR]
    client.app[MONITOR].alerts = Alerts(lambda: supervisor.settings, lambda _: None, sender)
    response = await client.post("/api/notify/test", headers=FETCH)
    assert response.status == 409 and (await response.json())["error"] == "no_notify_url"
    saved = await client.put("/api/settings", json={"notify_url": "https://ntfy.sh/x"}, headers=FETCH)
    assert saved.status == 200
    response = await client.post("/api/notify/test", headers=FETCH)
    assert response.status == 200 and sent == ["https://ntfy.sh/x"]
    fail = True
    response = await client.post("/api/notify/test", headers=FETCH)
    assert response.status == 502 and (await response.json())["error"] == "notify_failed"
    forbidden = NotifyError("forbidden address", "notify_address_forbidden")

    async def refusing(url: str, fmt: str, text: str, urgent: bool) -> None:
        raise forbidden

    client.app[MONITOR].alerts = Alerts(lambda: supervisor.settings, lambda _: None, refusing)
    response = await client.post("/api/notify/test", headers=FETCH)
    assert response.status == 400 and (await response.json())["error"] == "notify_address_forbidden"
    status = await (await client.get("/api/status")).json()
    assert status["alerts"] == []


async def test_alert_notes_api(make_client: ClientMaker, tmp_path: Path) -> None:
    """Notes are taken on known alerts only, and can be removed."""
    client = await make_client()
    response = await client.put("/api/alerts/ack", json={"key": "hot:sda", "acknowledged": True}, headers=FETCH)
    assert response.status == 404  # this app has no alerts
    supervisor = client.app[SUPERVISOR]
    alerts = Alerts(lambda: supervisor.settings, lambda _: None, store=client.app[MONITOR].history)
    client.app[MONITOR].alerts = alerts
    for body in ({"key": 1, "acknowledged": True}, {"key": "x"}, []):
        response = await client.put("/api/alerts/ack", json=body, headers=FETCH)
        assert response.status == 400
    response = await client.put("/api/alerts/ack", json={"key": "nothing", "acknowledged": True}, headers=FETCH)
    assert response.status == 404 and (await response.json())["error"] == "unknown_alert"
    await alerts.update({"hot:sda": ("drive_hot", {"drive": "sda", "temp": "51", "max": "50"})})
    response = await client.put("/api/alerts/ack", json={"key": "hot:sda", "acknowledged": True}, headers=FETCH)
    assert response.status == 200
    status = await (await client.get("/api/status")).json()
    assert status["alerts"][0]["acknowledged"] is True and "acknowledged" not in status
    await client.put("/api/alerts/ack", json={"key": "hot:sda", "acknowledged": False}, headers=FETCH)
    status = await (await client.get("/api/status")).json()
    assert status["alerts"][0]["acknowledged"] is False


async def test_metrics(make_client: ClientMaker) -> None:
    """The Prometheus text has the drives, the fans, SMART and the alerts; it needs the login like the API."""
    client = await make_client()
    await client.put("/api/settings", json={"drives": [DRIVE]}, headers=FETCH)
    await client.put("/api/labels", json={"key": PWM1, "label": 'Back "1"'}, headers=FETCH)
    client.app[MONITOR].history.save_smart(DRIVE, {"status": "warning", "warnings": ["crc_errors"]})
    response = await client.get("/metrics")
    assert response.status == 200 and response.content_type == "text/plain"
    text = await response.text()
    assert "hddfancontrol_up 1" in text
    assert 'hddfancontrol_drive_temperature_celsius{drive="sda"' in text and "} 41" in text
    assert 'label="Back \\"1\\""' in text  # quotes escaped
    assert f'hddfancontrol_smart_warnings{{device="{DRIVE}"}} 1' in text
    assert "# TYPE hddfancontrol_fan_rpm gauge" in text


async def test_history_csv(make_client: ClientMaker) -> None:
    """The history as a CSV download, with the names given to fans."""
    client = await make_client()
    await client.put("/api/labels", json={"key": PWM1, "label": "Back 1"}, headers=FETCH)
    history = client.app[MONITOR].history
    history.record(int(time.time()) - 60, [("pwm", PWM1, 50.0), ("temp", DRIVE, 41.5)])
    response = await client.get("/api/history.csv?hours=1")
    assert response.status == 200 and response.content_type == "text/csv"
    assert 'filename="hddfancontrol-history-1h.csv"' in response.headers["Content-Disposition"]
    lines = (await response.text()).splitlines()
    assert lines[0] == "time_utc,kind,name,label,value"
    assert any(line.endswith(f"pwm,{PWM1},Back 1,50.0") for line in lines)
    assert any(f"temp,{DRIVE},,41.5" in line for line in lines)
    assert (await client.get("/api/history.csv?hours=x")).status == 400
    assert (await client.get("/api/history.csv?hours=0")).status == 400
    # A name that a spreadsheet would run as a formula is written as text.
    await client.put("/api/labels", json={"key": PWM1, "label": "=HYPERLINK(1)"}, headers=FETCH)
    lines = (await (await client.get("/api/history.csv?hours=1")).text()).splitlines()
    assert any(line.endswith(f"pwm,{PWM1},'=HYPERLINK(1),50.0") for line in lines)


def test_csv_cells_that_start_a_formula() -> None:
    """=, +, -, @, tab and carriage return at the start of a text cell get a quote; numbers stay numbers."""
    from hddfancontrol_webui.web import _csv_cell

    assert [_csv_cell(v) for v in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "Back 1", "")] == [
        "'=1+1",
        "'+1",
        "'-1",
        "'@SUM(A1)",
        "'\tx",
        "'\rx",
        "Back 1",
        "",
    ]
    assert _csv_cell(-5.0) == -5.0


async def test_bays_api(make_client: ClientMaker) -> None:
    """The bay map is read and stored; a bad one is refused with its code."""
    client = await make_client()
    assert await (await client.get("/api/bays")).json() == {"rows": 0, "cols": 0, "slots": []}
    response = await client.put("/api/bays", json={"rows": 1, "cols": 2, "slots": [DRIVE, ""]}, headers=FETCH)
    assert response.status == 200 and (await response.json())["slots"] == [DRIVE, ""]
    bad = await client.put("/api/bays", json={"rows": 1, "cols": 1, "slots": ["/tmp/x"]}, headers=FETCH)
    assert bad.status == 400 and (await bad.json())["error"] == "invalid_dev_path"


async def test_cron_preview(make_client: ClientMaker) -> None:
    """The next runs or stretches of an expression, in the wall clock of the server; a wrong one is a 400."""
    client = await make_client()
    data = await (await client.get("/api/cron", params={"expr": "0 3 * * *"})).json()
    assert len(data["runs"]) == 3 and all(run.endswith("T03:00") for run in data["runs"]) and data["zone"]
    data = await (await client.get("/api/cron", params={"expr": "* 22-23,0-6 * * *", "windows": "1"})).json()
    assert len(data["windows"]) == 3 and all(end.endswith("T07:00") for _, end in data["windows"])
    data = await (await client.get("/api/cron", params={"expr": "0 0 29 2 mon"})).json()
    assert len(data["runs"]) == 3
    bad = await client.get("/api/cron", params={"expr": "0 24 * * *"})
    body = await bad.json()
    assert bad.status == 400 and body["error"] == "cron_range" and body["problems"][0]["params"]["max"] == 23
    odd = await client.get("/api/cron", params={"expr": "² * * * *"})
    assert odd.status == 400 and (await odd.json())["error"] == "cron_value"
    odd_settings = await client.put("/api/settings", json={"drive_temp_max": 10**400}, headers=FETCH)
    assert odd_settings.status == 400


async def test_cron_preview_far_runs(make_client: ClientMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs past the search limit are left out."""
    from hddfancontrol_webui import cron

    client = await make_client()
    monkeypatch.setattr(cron, "SEARCH_DAYS", 400)
    data = await (await client.get("/api/cron", params={"expr": "0 0 29 2 *"})).json()
    assert len(data["runs"]) <= 1


async def test_audit_log(make_client: ClientMaker) -> None:
    """Every change is logged with the user and address the gateway sent; reads are not."""
    client = await make_client()
    headers = {**FETCH, "X-Webui-User": "anto", "X-Webui-Client": "192.168.1.5"}
    await client.post("/api/restart", headers=headers)
    await client.put("/api/labels", json=[], headers=headers)
    await client.get("/api/settings", headers=headers)
    await client.put("/api/history.csv", headers=headers)
    lines = [line["text"] for line in client.app[SUPERVISOR].logs.since(0) if line["source"] == "audit"]
    assert lines[0] == "POST /api/restart by 'anto' from 192.168.1.5: 200"
    assert lines[1].startswith("PUT /api/labels by 'anto' from 192.168.1.5: 400")
    assert lines[2] == "PUT /api/history.csv by 'anto' from 192.168.1.5: 405"
    await client.post("/api/restart%0Afake", headers=headers)
    lines = [line["text"] for line in client.app[SUPERVISOR].logs.since(0) if line["source"] == "audit"]
    assert lines[3] == "POST /api/restart%0Afake by 'anto' from 192.168.1.5: 404" and len(lines) == 4

    async def broken() -> None:
        raise RuntimeError("broken")

    client.app[SUPERVISOR].restart = broken  # type: ignore[method-assign]  # an unexpected error of a handler
    assert (await client.post("/api/restart", headers=headers)).status == 500
    lines = [line["text"] for line in client.app[SUPERVISOR].logs.since(0) if line["source"] == "audit"]
    assert lines[4] == "POST /api/restart by 'anto' from 192.168.1.5: 500"


def test_audit_log_counts_repeats() -> None:
    """The same line again within a minute of when it was logged is counted; a steady stream still shows every minute."""
    from hddfancontrol_webui.web import AuditLog

    lines: list[str] = []
    now = [0.0]
    audit = AuditLog(lines.append, lambda: now[0])
    for _ in range(90):  # one a second for 90 s
        audit.write("PUT /api/manual/pwm by 'anto' from x: 200")
        now[0] += 1
    audit.write("PUT /api/manual by 'anto' from x: 200")
    audit.write("PUT /api/manual by 'anto' from x: 200")
    audit.flush()
    audit.flush()
    audit.write("PUT /api/manual by 'anto' from x: 200")  # after a flush: logged again
    assert lines == [
        "PUT /api/manual/pwm by 'anto' from x: 200",
        "PUT /api/manual/pwm by 'anto' from x: 200 (and 59 more like it)",
        "PUT /api/manual/pwm by 'anto' from x: 200",
        "PUT /api/manual/pwm by 'anto' from x: 200 (and 29 more like it)",
        "PUT /api/manual by 'anto' from x: 200",
        "PUT /api/manual by 'anto' from x: 200 (and 1 more like it)",
        "PUT /api/manual by 'anto' from x: 200",
    ]


async def test_audit_log_count_is_logged_when_its_minute_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the loop running, the count of a run is logged when its window ends, without waiting for another line."""
    from hddfancontrol_webui import web as web_module

    monkeypatch.setattr(web_module, "AUDIT_REPEAT_S", 0.05)
    lines: list[str] = []
    audit = web_module.AuditLog(lines.append)
    for _ in range(3):
        audit.write("POST /api/restart by 'anto' from x: 200")
    await asyncio.sleep(0.15)
    assert lines == ["POST /api/restart by 'anto' from x: 200", "POST /api/restart by 'anto' from x: 200 (and 2 more like it)"]


async def test_only_the_gateway_may_connect(make_client: ClientMaker, tmp_path: Path) -> None:
    """With peers, a connection from another user (or not over a Unix socket) is refused."""
    import os

    import aiohttp

    tcp = await make_client(peers=frozenset({os.getuid()}))
    refused = await tcp.get("/api/status")
    assert refused.status == 403 and (await refused.json())["error"] == "forbidden"
    app = tcp.app
    for peers, status in ((frozenset({os.getuid()}), 200), (frozenset({os.getuid() + 12345}), 403)):
        app = create_app(
            app[SUPERVISOR], app[HARDWARE], app[TEMP_LOG], app[MONITOR].history, app[MONITOR], app[LABELS], peers=peers
        )
        runner = web.AppRunner(app)
        await runner.setup()
        path = tmp_path / f"core-{status}.sock"
        await web.UnixSite(runner, str(path)).start()
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(path))) as session:
            async with session.get("http://core/healthz") as response:
                assert response.status == status
        await runner.cleanup()


async def test_support(make_client: ClientMaker) -> None:
    """The support request: the system, the host report, the hardware log lines with addresses masked."""
    client = await make_client()
    logs = client.server.app[SUPERVISOR].logs
    logs.add("sensors", "Loaded nct6775")
    logs.add("gateway", "Login of admin (from 192.168.1.20)")
    logs.add("webui", "Listening on 10.0.0.5 and fe80::1")
    data = await (await client.get("/api/support")).json()
    assert data["system"].startswith(("Docker image", "bare metal")) and "kernel" in data["system"]
    assert data["host"].startswith("== board")
    assert "[sensors] Loaded nct6775" in data["logs"] and "gateway" not in data["logs"]
    assert "[webui] Listening on <address> and <address>" in data["logs"]


async def test_support_hides_the_serial_numbers(make_client: ClientMaker) -> None:
    """A drive's /dev/disk/by-id name holds its serial number: the support request gives its kernel name, or <drive>."""
    client = await make_client()
    logs = client.server.app[SUPERVISOR].logs
    logs.add(
        "hddfancontrol", f"Starting: hddfancontrol daemon --drives {DRIVE} /dev/disk/by-id/ata-GONE_SERIAL9 --interval 20s"
    )
    logs.add("fan-control", "Own limits: ata-ST4000VN006-3CW104_ZW627CWW 30/40/50, nvme-eui.0025385b71b0a1c2 40/50/60")
    logs.add("webui", "The fan chip moved: /dev/disk/by-path/pci-0000:00:17.0-ata-1 stays")
    data = (await (await client.get("/api/support")).json())["logs"]
    assert "ZW627CWW" not in data and "GONE_SERIAL9" not in data and "0025385b71b0a1c2" not in data
    assert "--drives sda <drive> --interval" in data
    assert "Own limits: sda 30/40/50, <drive> 40/50/60" in data
    assert "/dev/disk/by-path/pci-" in data  # no serial in a path name


def test_mask_drives() -> None:
    """Every by-id name goes, with or without its folder; known ones become their kernel name."""
    names = {"/dev/disk/by-id/wwn-0x5000c500a1b2c3d4": "sdb"}
    text = "wwn-0x5000c500a1b2c3d4, /dev/disk/by-id/scsi-SATA_X_Y1, (usb-Kingston_ABC-0:0) and sda"
    assert mask_drives(text, names) == "sdb, <drive>, (<drive>) and sda"


def test_mask_addresses() -> None:
    """IPv4 and IPv6 addresses go; times, versions and chip names stay."""
    text = "from 192.168.1.20, fe80::1, 2a02:1:2:3:4::9 and [::1]:28462 at 10:17:02, v0.10.1, nct6775.656"
    assert (
        mask_addresses(text) == "from <address>, <address>, <address> and [<address>]:28462 at 10:17:02, v0.10.1, nct6775.656"
    )


async def test_archive_restore_and_delete_a_drive_that_is_gone(make_client: ClientMaker) -> None:
    """A monitored drive that is gone can be archived (data kept), restored, or deleted with all it left behind."""
    client = await make_client()
    gone = "/dev/disk/by-id/ata-GONE_1"
    alias = "/dev/disk/by-id/wwn-0x5000000000000009"
    other = "/dev/disk/by-id/ata-GONE_2"
    controlled = "/dev/disk/by-id/ata-GONE_3"
    document = {"drives": [controlled], "monitor_drives": [DRIVE, gone, other, controlled]}
    assert (await client.put("/api/settings", json=document, headers=FETCH)).status == 200
    monitor = client.app[MONITOR]
    history = monitor.history
    for device in (DRIVE, gone):
        history.save_smart(device, {"status": "ok", "model": "M"})
    history.record(int(time.time()), [("temp", alias, 30.0)])

    smart = await (await client.get("/api/smart")).json()
    assert smart[gone]["missing"] is True and "missing" not in smart[DRIVE]

    async def post(action: str, body: Any) -> tuple[int, Any]:
        response = await client.post(f"/api/drives/{action}", json=body, headers=FETCH)
        return response.status, await response.json()

    for body in ({}, {"device": 1}, {"device": "/etc/passwd"}, []):
        assert (await post("archive", body))[0] == 400
    assert await post("archive", {"device": "/dev/disk/by-id/ata-NONE"}) == (404, {"error": "unknown_drive", "problems": []})
    assert (await post("archive", {"device": DRIVE}))[1]["error"] == "drive_present"
    assert (await post("archive", {"device": controlled}))[1]["error"] == "drive_controlled"
    assert (await post("delete", {"device": DRIVE}))[0] == 409
    assert (await post("delete", {"device": controlled}))[0] == 409
    assert (await post("restore", {"device": gone}))[0] == 404

    status, answer = await post("archive", {"device": gone})
    assert status == 200 and answer[gone]["model"] == "M" and answer[gone]["present"] is False and answer[gone]["archived"]
    assert gone not in client.app[SUPERVISOR].settings.monitor_drives
    assert await (await client.get("/api/drives/archived")).json() == answer
    assert gone not in await (await client.get("/api/smart")).json()
    # The hourly tidy keeps the data of an archived drive.
    await asyncio.to_thread(monitor.tidy)
    assert gone in history.smart()

    status, answer = await post("restore", {"device": gone})
    assert status == 200 and answer == {}
    assert gone in client.app[SUPERVISOR].settings.monitor_drives

    history.remember_ids(gone, [gone, alias])
    await client.put("/api/labels", json={"key": alias, "label": "Old"}, headers=FETCH)
    await client.put("/api/bays", json={"rows": 1, "cols": 2, "slots": [gone, DRIVE]}, headers=FETCH)
    await post("archive", {"device": gone})
    status, answer = await post("delete", {"device": gone})
    assert status == 200 and answer == {}
    assert gone not in history.smart() and history.query(1)["series"] == []
    assert await (await client.get("/api/labels")).json() == {}
    assert (await (await client.get("/api/bays")).json())["slots"] == ["", DRIVE]

    # A monitored drive that is gone can be deleted without archiving it first.
    assert (await post("delete", {"device": other}))[0] == 200
    assert client.app[SUPERVISOR].settings.monitor_drives == (DRIVE, controlled)
    assert (await post("delete", {"device": other}))[0] == 404
