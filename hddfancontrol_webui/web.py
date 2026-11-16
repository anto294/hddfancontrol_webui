"""The web UI: static files and a small JSON API.

Security, in a container that runs privileged and drives the fans:

- With a username and password set (auth.py), every page and API call but
  the login page, its files and /healthz needs a session or HTTP Basic
  credentials. Without them the web UI is open, and says so.
- A request that changes something needs the X-Requested-With header and,
  when the browser sends one, an Origin that matches the host. A page of
  another site cannot send that header without a CORS preflight, which this
  server never accepts, so it cannot change anything.
- Every value is checked in settings.py; a PWM test or a manual fan value
  only goes to PWM files that the host really has.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .auth import SESSION_COOKIE, SESSION_LIFETIME, AuthError, AuthStore
from .const import HISTORY_MAX_HOURS, SOURCE_SENSORS, VERSION
from .hardware import Hardware
from .history import History
from .labels import LabelError, Labels
from .monitor import Monitor
from .sensors import SensorReport, scan_sensor_modules
from .settings import SettingsError, from_dict
from .supervisor import Busy, Supervisor
from .templog import TempLog

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CSRF_HEADER = "X-Requested-With"
AUTH_FAILURE_DELAY = 1.0
# Reachable without logging in: the login page needs them.
PUBLIC_PATHS = (
    "/healthz",
    "/login",
    "/api/login",
    "/static/style.css",
    "/static/login.js",
    "/static/i18n.js",
    "/static/favicon.png",
    "/static/logo.png",
    "/static/apple-touch-icon.png",
)
PUBLIC_PREFIXES = ("/static/i18n/",)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
SensorScan = Callable[[], Awaitable[SensorReport]]

SUPERVISOR: web.AppKey[Supervisor] = web.AppKey("supervisor", Supervisor)
HARDWARE: web.AppKey[Hardware] = web.AppKey("hardware", Hardware)
TEMP_LOG: web.AppKey[TempLog] = web.AppKey("temp_log", TempLog)
HISTORY: web.AppKey[History] = web.AppKey("history", History)
MONITOR: web.AppKey[Monitor] = web.AppKey("monitor", Monitor)
AUTH: web.AppKey[AuthStore] = web.AppKey("auth", AuthStore)
LABELS: web.AppKey[Labels] = web.AppKey("labels", Labels)
SENSOR_SCAN: web.AppKey[SensorScan] = web.AppKey("sensor_scan")
STATE: web.AppKey[dict[str, Any]] = web.AppKey("state", dict)


# ---------------------------
#   _json_error
# ---------------------------
def _json_error(status: int, code: str, problems: list[dict[str, Any]] | None = None) -> web.Response:
    """Build an error answer that the web UI translates by its code."""
    return web.json_response({"error": code, "problems": problems or []}, status=status)


# ---------------------------
#   security_headers
# ---------------------------
@web.middleware
async def security_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Forbid framing, inline scripts and content sniffing on every answer."""
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    else:
        # Keep the pages and static files, but ask every time (a cheap 304):
        # after an update the browser must not run the old app.js.
        response.headers.setdefault("Cache-Control", "no-cache")
    if isinstance(response, web.HTTPException):
        raise response
    return response


# ---------------------------
#   authentication
# ---------------------------
@web.middleware
async def authentication(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Let a request through with a session or Basic credentials, when a login is needed."""
    auth = request.app[AUTH]
    path = request.path
    if not auth.enabled or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await handler(request)
    if auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        return await handler(request)
    header = request.headers.get("Authorization", "")
    if header:
        if await asyncio.to_thread(auth.check_basic, header):
            return await handler(request)
        await asyncio.sleep(AUTH_FAILURE_DELAY)
    if path.startswith("/api/"):
        return _json_error(401, "unauthorized")
    raise web.HTTPFound("/login")


# ---------------------------
#   csrf
# ---------------------------
@web.middleware
async def csrf(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Refuse a change that a page of another site could have sent."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await handler(request)
    if request.headers.get(CSRF_HEADER) != "fetch":
        return _json_error(403, "csrf")
    origin = request.headers.get("Origin")
    if origin is not None and urlsplit(origin).netloc != request.host:
        return _json_error(403, "csrf")
    return await handler(request)


# ---------------------------
#   _read_json
# ---------------------------
async def _read_json(request: web.Request) -> Any:
    """Read the JSON body, or raise a 400 answer."""
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_json", "problems": []}), content_type="application/json"
        ) from err


# ---------------------------
#   index
# ---------------------------
async def index(request: web.Request) -> web.FileResponse:
    """Serve the page."""
    return web.FileResponse(STATIC_DIR / "index.html")


# ---------------------------
#   login_page
# ---------------------------
async def login_page(request: web.Request) -> web.StreamResponse:
    """Serve the login page, or the main page when no login is needed."""
    if not request.app[AUTH].enabled:
        raise web.HTTPFound("/")
    return web.FileResponse(STATIC_DIR / "login.html")


# ---------------------------
#   healthz
# ---------------------------
async def healthz(request: web.Request) -> web.Response:
    """Answer the health check of Docker. No login."""
    return web.Response(text="ok")


# ---------------------------
#   post_login
# ---------------------------
async def post_login(request: web.Request) -> web.Response:
    """Check the credentials and open a session."""
    auth = request.app[AUTH]
    data = await _read_json(request)
    username = data.get("username") if isinstance(data, dict) else None
    password = data.get("password") if isinstance(data, dict) else None
    if not isinstance(username, str) or not isinstance(password, str):
        return _json_error(400, "invalid_document")
    if not await asyncio.to_thread(auth.check, username, password):
        await asyncio.sleep(AUTH_FAILURE_DELAY)
        return _json_error(401, "wrong_credentials")
    response = web.json_response({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        auth.new_session(),
        max_age=SESSION_LIFETIME,
        httponly=True,
        samesite="Strict",
        secure=request.secure,
        path="/",
    )
    return response


# ---------------------------
#   post_logout
# ---------------------------
async def post_logout(request: web.Request) -> web.Response:
    """Close the session."""
    request.app[AUTH].end_session(request.cookies.get(SESSION_COOKIE))
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------
#   get_auth
# ---------------------------
async def get_auth(request: web.Request) -> web.Response:
    """Whether a login is needed, where the credentials come from, and the username."""
    return web.json_response(request.app[AUTH].status())


# ---------------------------
#   put_auth
# ---------------------------
async def put_auth(request: web.Request) -> web.Response:
    """Set or change the username and password. The caller logs in again."""
    auth = request.app[AUTH]
    data = await _read_json(request)
    if not isinstance(data, dict) or not all(isinstance(data.get(k, ""), str) for k in ("username", "password", "current")):
        return _json_error(400, "invalid_document")
    try:
        await asyncio.to_thread(
            auth.set_credentials, data.get("username", ""), data.get("password", ""), data.get("current", "")
        )
    except AuthError as err:
        if err.code == "wrong_password":
            await asyncio.sleep(AUTH_FAILURE_DELAY)
        return _json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the credentials: %s", err)
        return _json_error(500, "save_failed")
    response = web.json_response(auth.status())
    response.set_cookie(
        SESSION_COOKIE,
        auth.new_session(),
        max_age=SESSION_LIFETIME,
        httponly=True,
        samesite="Strict",
        secure=request.secure,
        path="/",
    )
    return response


# ---------------------------
#   delete_auth
# ---------------------------
async def delete_auth(request: web.Request) -> web.Response:
    """Remove the stored username and password."""
    auth = request.app[AUTH]
    data = await _read_json(request)
    current = data.get("current", "") if isinstance(data, dict) else ""
    if not isinstance(current, str):
        return _json_error(400, "invalid_document")
    try:
        await asyncio.to_thread(auth.clear_credentials, current)
    except AuthError as err:
        if err.code == "wrong_password":
            await asyncio.sleep(AUTH_FAILURE_DELAY)
        return _json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot delete the credentials: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response(auth.status())


# ---------------------------
#   get_status
# ---------------------------
async def get_status(request: web.Request) -> web.Response:
    """State of the daemons, the fans, the PWM test and the sensor modules."""
    app = request.app
    data = app[SUPERVISOR].status()
    report: SensorReport = app[STATE]["sensors"]
    data.update(
        version=VERSION,
        auth=app[AUTH].status(),
        sensors=report.to_dict(),
        latest=await asyncio.to_thread(app[TEMP_LOG].latest),
    )
    return web.json_response(data)


# ---------------------------
#   get_hardware
# ---------------------------
async def get_hardware(request: web.Request) -> web.Response:
    """Drives and hwmon devices of the host."""
    return web.json_response(await asyncio.to_thread(request.app[HARDWARE].snapshot))


# ---------------------------
#   get_history
# ---------------------------
async def get_history(request: web.Request) -> web.Response:
    """Temperatures and fan speeds of the last hours, from the database."""
    try:
        hours = float(request.query.get("hours", "6"))
    except ValueError:
        return _json_error(400, "invalid_number")
    if not 0 < hours <= HISTORY_MAX_HOURS:
        return _json_error(400, "out_of_range")
    return web.json_response(await asyncio.to_thread(request.app[HISTORY].query, hours))


# ---------------------------
#   get_smart
# ---------------------------
async def get_smart(request: web.Request) -> web.Response:
    """The last SMART data of every drive."""
    return web.json_response(await asyncio.to_thread(request.app[HISTORY].smart))


# ---------------------------
#   post_smart
# ---------------------------
async def post_smart(request: web.Request) -> web.Response:
    """Read the SMART data of every drive now. Sleeping drives are not woken."""
    return web.json_response(await request.app[MONITOR].refresh_smart())


# ---------------------------
#   get_labels
# ---------------------------
async def get_labels(request: web.Request) -> web.Response:
    """The names given to drives and fans."""
    return web.json_response(request.app[LABELS].all())


# ---------------------------
#   put_labels
# ---------------------------
async def put_labels(request: web.Request) -> web.Response:
    """Name a drive or a fan, or remove its name. Fan control is not restarted."""
    data = await _read_json(request)
    key = data.get("key") if isinstance(data, dict) else None
    label = data.get("label") if isinstance(data, dict) else None
    if not isinstance(key, str) or not isinstance(label, str):
        return _json_error(400, "invalid_document")
    try:
        labels = await asyncio.to_thread(request.app[LABELS].set, key, label)
    except LabelError as err:
        return _json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the labels: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response(labels)


# ---------------------------
#   get_settings
# ---------------------------
async def get_settings(request: web.Request) -> web.Response:
    """The settings in force and where they come from."""
    supervisor = request.app[SUPERVISOR]
    return web.json_response({"settings": supervisor.settings.to_dict(), "source": supervisor.source})


# ---------------------------
#   put_settings
# ---------------------------
async def put_settings(request: web.Request) -> web.Response:
    """Check, save and apply new settings."""
    supervisor = request.app[SUPERVISOR]
    try:
        settings = from_dict(await _read_json(request))
    except SettingsError as err:
        return _json_error(400, "invalid_settings", err.problems)
    try:
        await supervisor.apply(settings)
    except Busy:
        return _json_error(409, "busy")
    except OSError as err:
        _LOGGER.error("Cannot save the settings: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response({"settings": supervisor.settings.to_dict(), "source": supervisor.source})


# ---------------------------
#   delete_settings
# ---------------------------
async def delete_settings(request: web.Request) -> web.Response:
    """Delete the saved settings and use the environment again."""
    supervisor = request.app[SUPERVISOR]
    try:
        await supervisor.reset()
    except Busy:
        return _json_error(409, "busy")
    except OSError as err:
        _LOGGER.error("Cannot delete the settings: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response({"settings": supervisor.settings.to_dict(), "source": supervisor.source})


# ---------------------------
#   post_restart
# ---------------------------
async def post_restart(request: web.Request) -> web.Response:
    """Restart the daemons."""
    try:
        await request.app[SUPERVISOR].restart()
    except Busy:
        return _json_error(409, "busy")
    return web.json_response({"ok": True})


# ---------------------------
#   put_manual
# ---------------------------
async def put_manual(request: web.Request) -> web.Response:
    """Enter or leave manual control."""
    data = await _read_json(request)
    enabled = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(enabled, bool):
        return _json_error(400, "invalid_document")
    try:
        await request.app[SUPERVISOR].set_manual(enabled)
    except Busy:
        return _json_error(409, "busy")
    return web.json_response({"manual": enabled})


# ---------------------------
#   put_manual_pwm
# ---------------------------
async def put_manual_pwm(request: web.Request) -> web.Response:
    """Set one PWM output in manual control."""
    data = await _read_json(request)
    path = data.get("path") if isinstance(data, dict) else None
    value = data.get("value") if isinstance(data, dict) else None
    if not isinstance(path, str) or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        return _json_error(400, "invalid_document")
    if path not in await asyncio.to_thread(request.app[HARDWARE].pwm_paths):
        return _json_error(400, "unknown_pwm")
    try:
        await asyncio.to_thread(request.app[SUPERVISOR].set_pwm, path, value)
    except Busy:
        return _json_error(409, "not_manual")
    except OSError as err:
        return _json_error(500, "write_failed", [{"field": "path", "code": "write_failed", "params": {"error": str(err)}}])
    return web.json_response({"ok": True})


# ---------------------------
#   post_pwm_test
# ---------------------------
async def post_pwm_test(request: web.Request) -> web.Response:
    """Start a PWM test on PWM files that the host has."""
    data = await _read_json(request)
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return _json_error(400, "invalid_list")
    known = await asyncio.to_thread(request.app[HARDWARE].pwm_paths)
    unknown = [p for p in paths if p not in known]
    if unknown:
        return _json_error(400, "unknown_pwm", [{"field": "paths", "code": "unknown_pwm", "params": {"paths": unknown}}])
    try:
        await request.app[SUPERVISOR].start_pwm_test(list(dict.fromkeys(paths)))
    except Busy:
        return _json_error(409, "busy")
    return web.json_response({"ok": True})


# ---------------------------
#   delete_pwm_test
# ---------------------------
async def delete_pwm_test(request: web.Request) -> web.Response:
    """Cancel the PWM test."""
    await request.app[SUPERVISOR].cancel_pwm_test()
    return web.json_response({"ok": True})


# ---------------------------
#   post_sensor_scan
# ---------------------------
async def post_sensor_scan(request: web.Request) -> web.Response:
    """Scan for the sensor modules again, after loading them on the host for example."""
    state = request.app[STATE]
    if state["sensors"].state == "running":
        return _json_error(409, "busy")
    state["sensors"] = SensorReport(state="running")
    state["sensors"] = await request.app[SENSOR_SCAN]()
    return web.json_response(state["sensors"].to_dict())


# ---------------------------
#   get_logs
# ---------------------------
async def get_logs(request: web.Request) -> web.Response:
    """Log lines newer than 'after'."""
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        return _json_error(400, "invalid_number")
    logs = request.app[SUPERVISOR].logs
    return web.json_response({"lines": logs.since(after), "last": logs.last_seq})


# ---------------------------
#   create_app
# ---------------------------
def create_app(
    supervisor: Supervisor,
    hardware: Hardware,
    temp_log: TempLog,
    history: History,
    monitor: Monitor,
    auth: AuthStore,
    labels: Labels,
    sensor_scan: SensorScan | None = None,
) -> web.Application:
    """Build the application around parts that the caller starts and stops."""

    async def default_scan() -> SensorReport:
        return await scan_sensor_modules(lambda text: supervisor.logs.add(SOURCE_SENSORS, text))

    app = web.Application(middlewares=[security_headers, authentication, csrf], client_max_size=64 * 1024)
    app[SUPERVISOR] = supervisor
    app[HARDWARE] = hardware
    app[TEMP_LOG] = temp_log
    app[HISTORY] = history
    app[MONITOR] = monitor
    app[AUTH] = auth
    app[LABELS] = labels
    app[SENSOR_SCAN] = sensor_scan or default_scan
    app[STATE] = {"sensors": SensorReport()}
    router = app.router
    router.add_get("/", index)
    router.add_get("/login", login_page)
    router.add_get("/healthz", healthz)
    router.add_post("/api/login", post_login)
    router.add_post("/api/logout", post_logout)
    router.add_get("/api/auth", get_auth)
    router.add_put("/api/auth", put_auth)
    router.add_delete("/api/auth", delete_auth)
    router.add_get("/api/status", get_status)
    router.add_get("/api/hardware", get_hardware)
    router.add_get("/api/history", get_history)
    router.add_get("/api/smart", get_smart)
    router.add_post("/api/smart", post_smart)
    router.add_get("/api/labels", get_labels)
    router.add_put("/api/labels", put_labels)
    router.add_get("/api/settings", get_settings)
    router.add_put("/api/settings", put_settings)
    router.add_delete("/api/settings", delete_settings)
    router.add_post("/api/restart", post_restart)
    router.add_put("/api/manual", put_manual)
    router.add_put("/api/manual/pwm", put_manual_pwm)
    router.add_post("/api/pwm-test", post_pwm_test)
    router.add_delete("/api/pwm-test", delete_pwm_test)
    router.add_post("/api/sensors/scan", post_sensor_scan)
    router.add_get("/api/logs", get_logs)
    router.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
    return app
