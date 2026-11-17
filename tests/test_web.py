"""The HTTP API: login, CSRF protection, settings, fans, history, SMART."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from conftest import ScriptMaker

from hddfancontrol_webui.alerts import Alerts
from hddfancontrol_webui.auth import AuthStore
from hddfancontrol_webui.fancontrol import FanController, PwmWriter
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.history import History
from hddfancontrol_webui.labels import Labels
from hddfancontrol_webui.monitor import Monitor
from hddfancontrol_webui.notify import NotifyError
from hddfancontrol_webui.sensors import SensorReport
from hddfancontrol_webui.settings import SettingsStore
from hddfancontrol_webui.supervisor import LogBuffer, Supervisor
from hddfancontrol_webui.templog import TempLog
from hddfancontrol_webui.web import AUTH, MONITOR, SUPERVISOR, create_app

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
    response = await client.get("/healthz")
    assert response.status == 200
    login = await client.get("/login", allow_redirects=False)
    assert login.status == 302 and login.headers["Location"] == "/"
    status = await (await client.get("/api/status")).json()
    assert status["auth"]["enabled"] is False
    assert status["latest"]["temps"] == {"sda": 41.0}
    assert status["mode"] == "waiting"
    response = await client.get("/api/status")
    assert response.headers["Cache-Control"] == "no-store"
    missing = await client.get("/nope")
    assert missing.status == 404 and "Content-Security-Policy" in missing.headers


async def test_csrf(make_client: ClientMaker) -> None:
    """A change without the header, or from another origin, is refused."""
    client = await make_client()
    response = await client.post("/api/restart")
    assert response.status == 403
    response = await client.post("/api/restart", headers={**FETCH, "Origin": "http://evil.example"})
    assert response.status == 403
    response = await client.post("/api/restart", headers=FETCH)
    assert response.status == 200


async def test_login_flow(make_client: ClientMaker) -> None:
    """With credentials, pages redirect to the login and the API answers 401 until logged in."""
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    page = await client.get("/", allow_redirects=False)
    assert page.status == 302 and page.headers["Location"] == "/login"
    response = await client.get("/login")
    assert response.status == 200
    style = await client.get("/static/style.css")
    assert style.status == 200
    assert style.headers["Cache-Control"] == "no-cache"
    response = await client.get("/static/i18n/it.json")
    assert response.status == 200
    response = await client.get("/static/app.js", allow_redirects=False)
    assert response.status == 302
    unauthorized = await client.get("/api/status")
    assert unauthorized.status == 401
    assert unauthorized.headers["Cache-Control"] == "no-store"
    response = await client.get("/healthz")
    assert response.status == 200

    wrong = await client.post("/api/login", json={"username": "anto", "password": "nope"}, headers=FETCH)
    assert wrong.status == 401
    response = await client.post("/api/login", json=[], headers=FETCH)
    assert response.status == 400
    ok = await client.post("/api/login", json={"username": "anto", "password": "a-long-password"}, headers=FETCH)
    assert ok.status == 200
    cookie = ok.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    response = await client.get("/api/status")
    assert response.status == 200

    await client.post("/api/logout", headers=FETCH)
    response = await client.get("/api/status")
    assert response.status == 401


async def test_basic_auth(make_client: ClientMaker) -> None:
    """Scripts can send Basic credentials."""
    client = await make_client({"WEBUI_PASSWORD": "a-long-password"})
    good = "Basic " + base64.b64encode(b"admin:a-long-password").decode()
    bad = "Basic " + base64.b64encode(b"admin:x").decode()
    response = await client.get("/api/status", headers={"Authorization": good})
    assert response.status == 200
    response = await client.get("/api/status", headers={"Authorization": bad})
    assert response.status == 401


async def test_credentials_from_the_ui(make_client: ClientMaker) -> None:
    """Credentials set in the web UI protect it, and need the current password to change."""
    client = await make_client()
    bad = await client.put("/api/auth", json={"username": "anto", "password": "short"}, headers=FETCH)
    assert (await bad.json())["error"] == "password_too_short"
    response = await client.put("/api/auth", json={"username": 1}, headers=FETCH)
    assert response.status == 400
    ok = await client.put("/api/auth", json={"username": "anto", "password": "a-long-password"}, headers=FETCH)
    assert ok.status == 200 and (await ok.json())["source"] == "file"
    # The caller stays logged in with the new session.
    response = await client.get("/api/auth")
    assert response.status == 200
    wrong = await client.delete("/api/auth", json={"current": "nope"}, headers=FETCH)
    assert (await wrong.json())["error"] == "wrong_password"
    response = await client.delete("/api/auth", json={"current": 5}, headers=FETCH)
    assert response.status == 400
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
    smart = await (await client.post("/api/smart", headers=FETCH)).json()
    assert smart == {}
    nvme = "/dev/disk/by-id/nvme-Samsung_980_S64"
    response = await client.put("/api/settings", json={"monitor_drives": [nvme]}, headers=FETCH)
    assert response.status == 200
    smart = await (await client.post("/api/smart", headers=FETCH)).json()
    assert list(smart) == [nvme] and smart[nvme]["model"] == "M"
    smart = await (await client.get("/api/smart")).json()
    assert list(smart) == [nvme]
    response = await client.get("/api/logs?after=x")
    assert response.status == 400
    logs = await (await client.get("/api/logs?after=0")).json()
    assert "lines" in logs
    scan = await client.post("/api/sensors/scan", headers=FETCH)
    assert (await scan.json())["state"] == "done"


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


async def test_password_checks_run_one_at_a_time(make_client: ClientMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of logins never runs two scrypt checks at once."""
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    lock = threading.Lock()
    running = {"now": 0, "most": 0}

    def slow_check(username: str, password: str) -> bool:
        with lock:
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
        time.sleep(0.02)
        with lock:
            running["now"] -= 1
        return True

    monkeypatch.setattr(client.server.app[AUTH], "check", slow_check)
    body = {"username": "anto", "password": "a-long-password"}
    answers = await asyncio.gather(*(client.post("/api/login", json=body, headers=FETCH) for _ in range(5)))
    assert all(answer.status == 200 for answer in answers)
    assert running["most"] == 1


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
    status = await (await client.get("/api/status")).json()
    assert status["alerts"] == []


async def test_alert_notes_api(make_client: ClientMaker, tmp_path: Path) -> None:
    """Notes are taken on known alerts and warnings only, and can be removed."""
    client = await make_client()
    response = await client.put("/api/alerts/ack", json={"key": "banner:no_auth", "acknowledged": True}, headers=FETCH)
    assert response.status == 404  # this app has no alerts
    supervisor = client.app[SUPERVISOR]
    alerts = Alerts(lambda: supervisor.settings, lambda _: None, store=client.app[MONITOR].history)
    client.app[MONITOR].alerts = alerts
    for body in ({"key": 1, "acknowledged": True}, {"key": "x"}, []):
        response = await client.put("/api/alerts/ack", json=body, headers=FETCH)
        assert response.status == 400
    response = await client.put("/api/alerts/ack", json={"key": "nothing", "acknowledged": True}, headers=FETCH)
    assert response.status == 404 and (await response.json())["error"] == "unknown_alert"
    response = await client.put("/api/alerts/ack", json={"key": "banner:no_auth", "acknowledged": True}, headers=FETCH)
    assert response.status == 200
    status = await (await client.get("/api/status")).json()
    assert status["acknowledged"] == ["banner:no_auth"]
    await client.put("/api/alerts/ack", json={"key": "banner:no_auth", "acknowledged": False}, headers=FETCH)
    status = await (await client.get("/api/status")).json()
    assert status["acknowledged"] == []


async def test_too_many_wrong_passwords(make_client: ClientMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """An address that sends too many wrong passwords is refused without a check; a right one resets the count."""
    from hddfancontrol_webui import web as web_module

    monkeypatch.setattr(web_module, "AUTH_FAILURE_DELAY", 0)
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    limiter = client.app[web_module.LOGINS]
    monkeypatch.setattr(limiter, "_max", 2)
    good = {"username": "anto", "password": "a-long-password"}
    bad = {"username": "anto", "password": "nope"}
    assert (await client.post("/api/login", json=bad, headers=FETCH)).status == 401
    assert (await client.post("/api/login", json=good, headers=FETCH)).status == 200
    for _ in range(2):
        assert (await client.post("/api/login", json=bad, headers=FETCH)).status == 401
    blocked = await client.post("/api/login", json=good, headers=FETCH)
    assert blocked.status == 429 and (await blocked.json())["error"] == "too_many_attempts"
    client.session.cookie_jar.clear()  # no session: only the Basic credentials count
    basic = "Basic " + base64.b64encode(b"anto:a-long-password").decode()
    response = await client.get("/api/status", headers={"Authorization": basic})
    assert response.status == 429
    limiter.succeeded("127.0.0.1")
    response = await client.get("/api/status", headers={"Authorization": basic})
    assert response.status == 200


def test_login_limiter_window_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures expire after the window; the table of addresses stays bounded."""
    from hddfancontrol_webui import web as web_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(web_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(web_module, "LOGIN_MAX_ADDRESSES", 2)
    limiter = web_module.LoginLimiter(max_failures=1, window=10)
    limiter.failed("a")
    assert limiter.blocked("a")
    clock["now"] += 11
    assert not limiter.blocked("a")
    limiter.failed("b")
    limiter.failed("c")
    limiter.failed("d")  # the table is full of recent failures: not counted
    assert limiter.blocked("b") and limiter.blocked("c") and not limiter.blocked("d")
    clock["now"] += 11
    limiter.failed("d")  # expired entries make room
    assert limiter.blocked("d")


async def test_session_cookie_is_secure_behind_an_https_proxy(make_client: ClientMaker) -> None:
    """The session cookie is marked Secure when a reverse proxy says the browser used HTTPS."""
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    good = {"username": "anto", "password": "a-long-password"}
    plain = await client.post("/api/login", json=good, headers=FETCH)
    assert "Secure" not in plain.headers["Set-Cookie"]
    proxied = await client.post("/api/login", json=good, headers={**FETCH, "X-Forwarded-Proto": "https, http"})
    assert "Secure" in proxied.headers["Set-Cookie"]


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


async def test_metrics_need_the_login(make_client: ClientMaker) -> None:
    """With credentials set, /metrics needs them (HTTP Basic works for Prometheus)."""
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    assert (await client.get("/metrics")).status == 401
    basic = "Basic " + base64.b64encode(b"anto:a-long-password").decode()
    assert (await client.get("/metrics", headers={"Authorization": basic})).status == 200


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


async def test_web_app_manifest(make_client: ClientMaker) -> None:
    """The manifest and its icons are public (the browser fetches them without the session) and typed."""
    client = await make_client({"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"})
    response = await client.get("/static/manifest.webmanifest")
    assert response.status == 200 and response.content_type == "application/manifest+json"
    manifest = json.loads(await response.text())
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for item in manifest["icons"]:
        assert (await client.get(item["src"], allow_redirects=False)).status == 200


async def test_bays_api(make_client: ClientMaker) -> None:
    """The bay map is read and stored; a bad one is refused with its code."""
    client = await make_client()
    assert await (await client.get("/api/bays")).json() == {"rows": 0, "cols": 0, "slots": []}
    response = await client.put("/api/bays", json={"rows": 1, "cols": 2, "slots": [DRIVE, ""]}, headers=FETCH)
    assert response.status == 200 and (await response.json())["slots"] == [DRIVE, ""]
    bad = await client.put("/api/bays", json={"rows": 1, "cols": 1, "slots": ["/tmp/x"]}, headers=FETCH)
    assert bad.status == 400 and (await bad.json())["error"] == "invalid_dev_path"
