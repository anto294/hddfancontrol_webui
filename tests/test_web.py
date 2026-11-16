"""The HTTP API: login, CSRF protection, settings, fans, history, SMART."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from conftest import ScriptMaker

from hddfancontrol_webui.auth import AuthStore
from hddfancontrol_webui.fancontrol import FanController, PwmWriter
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.history import History
from hddfancontrol_webui.labels import Labels
from hddfancontrol_webui.monitor import Monitor
from hddfancontrol_webui.sensors import SensorReport
from hddfancontrol_webui.settings import SettingsStore
from hddfancontrol_webui.supervisor import LogBuffer, Supervisor
from hddfancontrol_webui.templog import TempLog
from hddfancontrol_webui.web import create_app

PWM1 = "/sys/class/hwmon/hwmon2/pwm1"
DRIVE = "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
FETCH = {"X-Requested-With": "fetch"}

Client = TestClient[Any, Any]
ClientMaker = Callable[..., Awaitable[Client]]


@pytest.fixture
def make_client(aiohttp_client: Any, tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker) -> ClientMaker:
    """Build a client on an app with fake hardware and programs."""

    async def make(env: dict[str, str] | None = None) -> Client:
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
            SettingsStore(config, {}),
            logs,
            log_path,
            fans,
            hddfancontrol=make_script("hddfancontrol", "time.sleep(60)"),
            hdidle=make_script("hd-idle", "time.sleep(60)"),
        )
        hardware = Hardware(fake_dev, fake_sys)
        history = History(config / "history.db")

        async def smart_reader(device: str) -> dict[str, Any]:
            return {"status": "ok", "model": "M"}

        monitor = Monitor(history, temp_log, hardware, lambda: supervisor.settings, smart_reader)

        async def scan() -> SensorReport:
            return SensorReport(state="done")

        app = create_app(supervisor, hardware, temp_log, history, monitor, AuthStore(config, env or {}), Labels(config), scan)

        async def stop(app: web.Application) -> None:
            await supervisor.stop()
            history.close()

        app.on_cleanup.append(stop)
        client: Client = await aiohttp_client(app)
        return client

    return make


async def test_open_ui_and_headers(make_client: ClientMaker) -> None:
    """Without a password the UI answers, with the security headers."""
    client = await make_client()
    response = await client.get("/")
    assert response.status == 200
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert (await client.get("/healthz")).status == 200
    login = await client.get("/login", allow_redirects=False)
    assert login.status == 302 and login.headers["Location"] == "/"
    status = await (await client.get("/api/status")).json()
    assert status["auth"]["enabled"] is False
    assert status["latest"]["temps"] == {"sda": 41.0}
    assert status["mode"] == "waiting"
    assert (await client.get("/api/status")).headers["Cache-Control"] == "no-store"
    missing = await client.get("/nope")
    assert missing.status == 404 and "Content-Security-Policy" in missing.headers


async def test_csrf(make_client: ClientMaker) -> None:
    """A change without the header, or from another origin, is refused."""
    client = await make_client()
    assert (await client.post("/api/restart")).status == 403
    assert (await client.post("/api/restart", headers={**FETCH, "Origin": "http://evil.example"})).status == 403
    assert (await client.post("/api/restart", headers=FETCH)).status == 200


async def test_login_flow(make_client: ClientMaker) -> None:
    """With credentials, pages redirect to the login and the API answers 401 until logged in."""
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    page = await client.get("/", allow_redirects=False)
    assert page.status == 302 and page.headers["Location"] == "/login"
    assert (await client.get("/login")).status == 200
    style = await client.get("/static/style.css")
    assert style.status == 200
    assert style.headers["Cache-Control"] == "no-cache"
    assert (await client.get("/static/i18n/it.json")).status == 200
    assert (await client.get("/static/app.js", allow_redirects=False)).status == 302
    unauthorized = await client.get("/api/status")
    assert unauthorized.status == 401
    assert unauthorized.headers["Cache-Control"] == "no-store"
    assert (await client.get("/healthz")).status == 200

    wrong = await client.post("/api/login", json={"username": "anto", "password": "nope"}, headers=FETCH)
    assert wrong.status == 401
    assert (await client.post("/api/login", json=[], headers=FETCH)).status == 400
    ok = await client.post("/api/login", json={"username": "anto", "password": "a-long-password"}, headers=FETCH)
    assert ok.status == 200
    cookie = ok.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert (await client.get("/api/status")).status == 200

    await client.post("/api/logout", headers=FETCH)
    assert (await client.get("/api/status")).status == 401


async def test_basic_auth(make_client: ClientMaker) -> None:
    """Scripts can send Basic credentials."""
    client = await make_client({"WEBUI_PASSWORD": "a-long-password"})
    good = "Basic " + base64.b64encode(b"admin:a-long-password").decode()
    bad = "Basic " + base64.b64encode(b"admin:x").decode()
    assert (await client.get("/api/status", headers={"Authorization": good})).status == 200
    assert (await client.get("/api/status", headers={"Authorization": bad})).status == 401


async def test_credentials_from_the_ui(make_client: ClientMaker) -> None:
    """Credentials set in the web UI protect it, and need the current password to change."""
    client = await make_client()
    bad = await client.put("/api/auth", json={"username": "anto", "password": "short"}, headers=FETCH)
    assert (await bad.json())["error"] == "password_too_short"
    assert (await client.put("/api/auth", json={"username": 1}, headers=FETCH)).status == 400
    ok = await client.put("/api/auth", json={"username": "anto", "password": "a-long-password"}, headers=FETCH)
    assert ok.status == 200 and (await ok.json())["source"] == "file"
    # The caller stays logged in with the new session.
    assert (await client.get("/api/auth")).status == 200
    wrong = await client.delete("/api/auth", json={"current": "nope"}, headers=FETCH)
    assert (await wrong.json())["error"] == "wrong_password"
    assert (await client.delete("/api/auth", json={"current": 5}, headers=FETCH)).status == 400
    removed = await client.delete("/api/auth", json={"current": "a-long-password"}, headers=FETCH)
    assert (await removed.json())["enabled"] is False


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

    doc = {"drives": [DRIVE], "pwms": [{"path": PWM1, "start": 200, "stop": 75}]}
    saved = await client.put("/api/settings", json=doc, headers=FETCH)
    assert saved.status == 200 and (await saved.json())["source"] == "file"
    status = await (await client.get("/api/status")).json()
    assert status["mode"] == "running"

    reset = await client.delete("/api/settings", headers=FETCH)
    assert (await reset.json())["source"] == "environment"


async def test_manual_api(make_client: ClientMaker, fake_sys: Path) -> None:
    """Manual control only accepts real PWM files and valid values."""
    client = await make_client()
    assert (await client.put("/api/manual/pwm", json={"path": PWM1, "value": 10}, headers=FETCH)).status == 409
    assert (await client.put("/api/manual", json={"enabled": "yes"}, headers=FETCH)).status == 400
    assert (await client.put("/api/manual", json={"enabled": True}, headers=FETCH)).status == 200
    assert (
        await client.put("/api/manual/pwm", json={"path": "/sys/class/hwmon/hwmon2/name", "value": 1}, headers=FETCH)
    ).status == 400
    assert (await client.put("/api/manual/pwm", json={"path": PWM1, "value": 256}, headers=FETCH)).status == 400
    assert (await client.put("/api/manual/pwm", json={"path": PWM1, "value": True}, headers=FETCH)).status == 400
    assert (await client.put("/api/manual/pwm", json={"path": PWM1, "value": 77}, headers=FETCH)).status == 200
    assert (fake_sys / "class/hwmon/hwmon2/pwm1").read_text().strip() == "77"
    (fake_sys / "class/hwmon/hwmon2/pwm2").chmod(0o400)
    denied = await client.put("/api/manual/pwm", json={"path": "/sys/class/hwmon/hwmon2/pwm2", "value": 1}, headers=FETCH)
    assert denied.status in (200, 500)
    assert (await client.put("/api/manual", json={"enabled": False}, headers=FETCH)).status == 200


async def test_pwm_test_api(make_client: ClientMaker) -> None:
    """Only PWM files of the host can be tested, and a second test is refused."""
    client = await make_client()
    assert (await client.post("/api/pwm-test", json={"paths": []}, headers=FETCH)).status == 400
    unknown = await client.post("/api/pwm-test", json={"paths": ["/sys/power/state"]}, headers=FETCH)
    assert (await unknown.json())["error"] == "unknown_pwm"
    assert (await client.post("/api/pwm-test", json={"paths": [PWM1]}, headers=FETCH)).status == 200
    assert (await client.post("/api/pwm-test", json={"paths": [PWM1]}, headers=FETCH)).status == 409
    assert (await client.put("/api/settings", json={}, headers=FETCH)).status == 409
    assert (await client.delete("/api/settings", headers=FETCH)).status == 409
    assert (await client.put("/api/manual", json={"enabled": True}, headers=FETCH)).status == 409
    assert (await client.post("/api/restart", headers=FETCH)).status == 409
    assert (await client.delete("/api/pwm-test", headers=FETCH)).status == 200


async def test_read_endpoints(make_client: ClientMaker) -> None:
    """Hardware, history, SMART, logs and the sensor scan."""
    client = await make_client()
    hardware = await (await client.get("/api/hardware")).json()
    assert hardware["hwmons"][0]["pwms"][0]["path"] == PWM1
    assert (await client.get("/api/history?hours=x")).status == 400
    assert (await client.get("/api/history?hours=0")).status == 400
    assert "series" in await (await client.get("/api/history?hours=24")).json()
    assert await (await client.get("/api/smart")).json() == {}
    smart = await (await client.post("/api/smart", headers=FETCH)).json()
    assert smart["/dev/disk/by-id/nvme-Samsung_980_S64"]["model"] == "M"
    assert (await client.get("/api/logs?after=x")).status == 400
    logs = await (await client.get("/api/logs?after=0")).json()
    assert "lines" in logs
    scan = await client.post("/api/sensors/scan", headers=FETCH)
    assert (await scan.json())["state"] == "done"


async def test_labels_api(make_client: ClientMaker) -> None:
    """Labels are set without restarting anything, and checked."""
    client = await make_client()
    assert await (await client.get("/api/labels")).json() == {}
    saved = await client.put("/api/labels", json={"key": PWM1, "label": "Front fan"}, headers=FETCH)
    assert await saved.json() == {PWM1: "Front fan"}
    bad = await client.put("/api/labels", json={"key": "/etc/x", "label": "a"}, headers=FETCH)
    assert (await bad.json())["error"] == "invalid_label_key"
    assert (await client.put("/api/labels", json={"key": PWM1}, headers=FETCH)).status == 400


# ---------------------------
#   test_static_files_need_no_inline_style
# ---------------------------
def test_static_files_need_no_inline_style() -> None:
    """The pages and scripts set no style attribute, which the CSP would block."""
    static = Path(__file__).parent.parent / "hddfancontrol_webui" / "static"
    pattern = re.compile(r"""\bstyle\s*[:=]|setAttribute\(\s*["']style|<style""")
    for path in [*static.glob("*.js"), *static.glob("*.html")]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name
