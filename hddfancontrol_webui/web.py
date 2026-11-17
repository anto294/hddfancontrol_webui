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
import csv
import io
import json
import logging
import mimetypes
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .auth import SESSION_COOKIE, SESSION_LIFETIME, AuthError, AuthStore
from .bays import BayError, Bays
from .const import HISTORY_MAX_HOURS, SOURCE_SENSORS, VERSION
from .cron import CronError, parse, windows
from .hardware import Hardware
from .history import History
from .labels import LabelError, Labels
from .monitor import Monitor
from .notify import NotifyError
from .sensors import SensorReport, scan_sensor_modules
from .settings import SettingsError, from_dict
from .supervisor import Busy, ReadOnly, Supervisor
from .templog import TempLog

_LOGGER = logging.getLogger(__name__)

# The web app manifest (install on a phone's home screen); Python does not know its type.
mimetypes.add_type("application/manifest+json", ".webmanifest")

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
    "/static/manifest.webmanifest",
    "/static/icon-512.png",
)
PUBLIC_PREFIXES = ("/static/i18n/",)
# After this many wrong passwords from one address within LOGIN_WINDOW
# seconds, that address is refused without checking until the window ends.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW = 900.0
LOGIN_MAX_ADDRESSES = 10000

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
SensorScan = Callable[[], Awaitable[SensorReport]]

SUPERVISOR: web.AppKey[Supervisor] = web.AppKey("supervisor", Supervisor)
HARDWARE: web.AppKey[Hardware] = web.AppKey("hardware", Hardware)
TEMP_LOG: web.AppKey[TempLog] = web.AppKey("temp_log", TempLog)
HISTORY: web.AppKey[History] = web.AppKey("history", History)
MONITOR: web.AppKey[Monitor] = web.AppKey("monitor", Monitor)
AUTH: web.AppKey[AuthStore] = web.AppKey("auth", AuthStore)
LABELS: web.AppKey[Labels] = web.AppKey("labels", Labels)
BAYS: web.AppKey[Bays] = web.AppKey("bays", Bays)
SENSOR_SCAN: web.AppKey[SensorScan] = web.AppKey("sensor_scan")
STATE: web.AppKey[dict[str, Any]] = web.AppKey("state", dict)
PASSWORD_CHECKS: web.AppKey[asyncio.Semaphore] = web.AppKey("password_checks", asyncio.Semaphore)


# ---------------------------
#   LoginLimiter
# ---------------------------
class LoginLimiter:
    """Count the wrong passwords of each client address, and refuse one that sends too many.

    The scrypt checks already run one at a time; this stops one address
    from guessing for hours, and from keeping the checks busy for everyone
    else. Behind a reverse proxy every client has the address of the proxy.
    """

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW) -> None:
        """Allow max_failures wrong passwords per address within window seconds."""
        self._max = max_failures
        self._window = window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, address: str, now: float) -> list[float]:
        """The failures of address within the window."""
        recent = [t for t in self._failures.get(address, []) if now - t < self._window]
        if recent:
            self._failures[address] = recent
        else:
            self._failures.pop(address, None)
        return recent

    def blocked(self, address: str) -> bool:
        """Whether address sent too many wrong passwords lately."""
        return len(self._recent(address, time.monotonic())) >= self._max

    def failed(self, address: str) -> None:
        """Count a wrong password of address."""
        now = time.monotonic()
        if address not in self._failures and len(self._failures) >= LOGIN_MAX_ADDRESSES:
            for other in list(self._failures):
                self._recent(other, now)
            if len(self._failures) >= LOGIN_MAX_ADDRESSES:
                return
        self._failures.setdefault(address, []).append(now)

    def succeeded(self, address: str) -> None:
        """Forget the failures of address after a right password."""
        self._failures.pop(address, None)


LOGINS: web.AppKey[LoginLimiter] = web.AppKey("logins", LoginLimiter)


# ---------------------------
#   _password_work
# ---------------------------
async def _password_work(request: web.Request, func: Callable[..., Any], *args: Any) -> Any:
    """Run a password check or change in a thread, one at a time.

    Each scrypt hash takes 16 MiB. Without a limit, a burst of wrong logins
    (no session needed) runs one per worker thread and can push the
    container past its memory limit, and an OOM kill leaves the fans
    without control. One at a time also slows down guessing.
    """
    async with request.app[PASSWORD_CHECKS]:
        return await asyncio.to_thread(func, *args)


# ---------------------------
#   _is_https
# ---------------------------
def _is_https(request: web.Request) -> bool:
    """Whether the browser reached the web UI over HTTPS, directly or through a reverse proxy.

    The proxy says so in X-Forwarded-Proto. Trusting it only decides
    whether the session cookie is marked Secure: a client that fakes it
    gets a cookie its own browser does not send back over HTTP.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    return request.secure or forwarded == "https"


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
        logins = request.app[LOGINS]
        address = request.remote or ""
        if logins.blocked(address):
            return _json_error(429, "too_many_attempts")
        if await _password_work(request, auth.check_basic, header):
            logins.succeeded(address)
            return await handler(request)
        logins.failed(address)
        await asyncio.sleep(AUTH_FAILURE_DELAY)
    # Programs (the API, a Prometheus scraper) get 401; a browser goes to the login page.
    if path.startswith("/api/") or path == "/metrics":
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
    logins = request.app[LOGINS]
    address = request.remote or ""
    if logins.blocked(address):
        return _json_error(429, "too_many_attempts")
    if not await _password_work(request, auth.check, username, password):
        logins.failed(address)
        await asyncio.sleep(AUTH_FAILURE_DELAY)
        return _json_error(401, "wrong_credentials")
    logins.succeeded(address)
    response = web.json_response({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        auth.new_session(),
        max_age=SESSION_LIFETIME,
        httponly=True,
        samesite="Strict",
        secure=_is_https(request),
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
        await _password_work(
            request, auth.set_credentials, data.get("username", ""), data.get("password", ""), data.get("current", "")
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
        secure=_is_https(request),
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
        await _password_work(request, auth.clear_credentials, current)
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
    # The pages ask for the status every few seconds while they are open.
    app[SUPERVISOR].touch_manual()
    data = app[SUPERVISOR].status()
    alerts = app[MONITOR].alerts
    report: SensorReport = app[STATE]["sensors"]
    data.update(
        version=VERSION,
        auth=app[AUTH].status(),
        sensors=report.to_dict(),
        latest=await asyncio.to_thread(app[TEMP_LOG].latest),
        alerts=alerts.status() if alerts is not None else [],
        acknowledged=[key for key in alerts.acks if key.startswith("banner:")] if alerts is not None else [],
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
#   get_history_csv
# ---------------------------
# A CSV never holds more than this many points per series (a year of 30 s samples is a million).
CSV_MAX_POINTS = 20000
# How many runs or stretches the preview of a cron expression shows.
CRON_PREVIEW = 3


async def get_history_csv(request: web.Request) -> web.Response:
    """The history of the last hours as CSV: time (UTC), kind, name, label, value."""
    try:
        hours = float(request.query.get("hours", "24"))
    except ValueError:
        return _json_error(400, "invalid_number")
    if not 0 < hours <= HISTORY_MAX_HOURS:
        return _json_error(400, "out_of_range")
    data = await asyncio.to_thread(request.app[HISTORY].query, hours, None, CSV_MAX_POINTS)
    labels = request.app[LABELS].all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time_utc", "kind", "name", "label", "value"])
    for series in data["series"]:
        label = labels.get(series["name"], "")
        for ts, value in series["points"]:
            when = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            writer.writerow([when, series["kind"], series["name"], label, value])
    name = f"hddfancontrol-history-{hours:g}h.csv"
    return web.Response(
        text=buffer.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---------------------------
#   get_smart
# ---------------------------
async def get_smart(request: web.Request) -> web.Response:
    """The last SMART data of the monitored drives."""
    return web.json_response(await asyncio.to_thread(request.app[MONITOR].smart))


# ---------------------------
#   post_smart
# ---------------------------
async def post_smart(request: web.Request) -> web.Response:
    """Read the SMART data of the monitored drives now. Sleeping drives are not woken."""
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
#   get_bays
# ---------------------------
async def get_bays(request: web.Request) -> web.Response:
    """Where each drive sits in the case."""
    return web.json_response(request.app[BAYS].get())


# ---------------------------
#   put_bays
# ---------------------------
async def put_bays(request: web.Request) -> web.Response:
    """Store the bay map: {rows, cols, slots}. Fan control is not restarted."""
    data = await _read_json(request)
    try:
        bays = await asyncio.to_thread(request.app[BAYS].set, data)
    except BayError as err:
        return _json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the bay map: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response(bays)


# ---------------------------
#   get_cron
# ---------------------------
async def get_cron(request: web.Request) -> web.Response:
    """Check a cron expression (?expr=) and give its next runs, or with ?windows=1 its next stretches.

    For the preview under the schedule fields: {runs: [time, ...]} or
    {windows: [[start, end], ...]}, with zone. The times are the local
    wall clock of the server ("2026-11-22T03:00"), which the schedules
    follow, not the time zone of the browser. A wrong expression is a 400
    with the problem of the settings check.
    """
    expression = " ".join(request.query.get("expr", "").split())
    try:
        cron = parse(expression)
    except CronError as err:
        return _json_error(400, err.code, [{"field": "expr", "code": err.code, "params": err.params}])
    now = time.time()

    def wall(stamp: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M", time.localtime(stamp))

    zone = time.strftime("%Z", time.localtime(now))
    if request.query.get("windows") == "1":
        found = await asyncio.to_thread(windows, cron, now, CRON_PREVIEW)
        return web.json_response({"windows": [[wall(start), wall(end)] for start, end in found], "zone": zone})
    runs: list[str] = []
    after: float | None = now
    while after is not None and len(runs) < CRON_PREVIEW:
        after = cron.next(after)
        if after is not None:
            runs.append(wall(after))
    return web.json_response({"runs": runs, "zone": zone})


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
#   put_alert_ack
# ---------------------------
async def put_alert_ack(request: web.Request) -> web.Response:
    """Take note of an alert ({"key", "acknowledged": true}) or show it again (false)."""
    alerts = request.app[MONITOR].alerts
    data = await _read_json(request)
    if not isinstance(data, dict) or not isinstance(data.get("key"), str) or not isinstance(data.get("acknowledged"), bool):
        return _json_error(400, "invalid_document")
    if alerts is None:
        return _json_error(404, "unknown_alert")
    try:
        if data["acknowledged"]:
            if not await asyncio.to_thread(alerts.ack, data["key"]):
                return _json_error(404, "unknown_alert")
        else:
            await asyncio.to_thread(alerts.unack, data["key"])
    except OSError as err:
        _LOGGER.error("Cannot save the notes on alerts: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response({"ok": True})


# ---------------------------
#   post_notify_test
# ---------------------------
async def post_notify_test(request: web.Request) -> web.Response:
    """Send a test notification with the saved settings."""
    alerts = request.app[MONITOR].alerts
    if alerts is None or not request.app[SUPERVISOR].settings.notify_url:
        return _json_error(409, "no_notify_url")
    try:
        await alerts.test()
    except NotifyError as err:
        _LOGGER.warning("Test notification failed: %s", err)
        return _json_error(502, "notify_failed")
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
    except ReadOnly:
        return _json_error(409, "read_only")
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
    except ReadOnly:
        return _json_error(409, "read_only")
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
#   _metric_labels
# ---------------------------
def _metric_labels(**labels: Any) -> str:
    """Prometheus labels, escaped: {name="value",...}."""
    parts = []
    for name, value in labels.items():
        text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{name}="{text}"')
    return "{" + ",".join(parts) + "}"


# ---------------------------
#   get_metrics
# ---------------------------
async def get_metrics(request: web.Request) -> web.Response:
    """The state in the Prometheus text format, for Prometheus or Grafana Agent (behind the login, like the API)."""
    app = request.app
    supervisor = app[SUPERVISOR]
    status = supervisor.status()
    latest = await asyncio.to_thread(app[TEMP_LOG].latest)
    hwmons = await asyncio.to_thread(app[HARDWARE].hwmons)
    drives = await asyncio.to_thread(app[HARDWARE].drives)
    smart = await asyncio.to_thread(app[MONITOR].smart)
    labels = app[LABELS].all()
    alerts = app[MONITOR].alerts
    chosen = {pwm.path for pwm in supervisor.settings.pwms}
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[dict[str, Any], float]]) -> None:
        lines.append(f"# HELP hddfancontrol_{name} {help_text}")
        lines.append(f"# TYPE hddfancontrol_{name} {kind}")
        for sample_labels, value in samples:
            lines.append(f"hddfancontrol_{name}{_metric_labels(**sample_labels) if sample_labels else ''} {value:g}")

    metric("up", "gauge", "The web UI runs.", [({}, 1)])
    metric(
        "info",
        "gauge",
        "Version and modes.",
        [({"version": VERSION, "mode": status["mode"], "control_mode": status["control_mode"]}, 1)],
    )
    metric(
        "failsafe",
        "gauge",
        "1 when the fans run at full speed for safety.",
        [({}, int(bool(status["fans"]["auto"]["failsafe"] or status["fans"]["safety"] or status["fans"]["error"])))],
    )
    by_name = {drive.name: drive for drive in drives}
    temps: list[tuple[dict[str, Any], float]] = []
    asleep: list[tuple[dict[str, Any], float]] = []
    if latest:
        settings = supervisor.settings
        count = max(0, len(latest["temps"]) - len(settings.hwmons))
        for name, value in list(latest["temps"].items())[:count]:
            kernel = name.split()[0]
            drive = by_name.get(kernel)
            label = next((labels[i] for i in (drive.ids if drive else []) if i in labels), "")
            sample = {"drive": kernel, "model": drive.model if drive else " ".join(name.split()[1:]), "label": label}
            asleep.append((sample, int(value is None)))
            if value is not None:
                temps.append((sample, value))
    metric("drive_temperature_celsius", "gauge", "Drive temperature measured by hddfancontrol.", temps)
    metric("drive_asleep", "gauge", "1 when the drive sleeps (or its probe failed).", asleep)
    pwm_samples: list[tuple[dict[str, Any], float]] = []
    rpm_samples: list[tuple[dict[str, Any], float]] = []
    for device in hwmons:
        for pwm in device.pwms:
            sample = {
                "fan": pwm.path,
                "chip": device.name,
                "label": labels.get(pwm.path, ""),
                "chosen": str(pwm.path in chosen).lower(),
            }
            if pwm.value is not None:
                pwm_samples.append((sample, round(pwm.value * 100 / 255, 1)))
            if pwm.rpm is not None:
                rpm_samples.append((sample, pwm.rpm))
    metric("fan_pwm_percent", "gauge", "PWM value of each output, in percent.", pwm_samples)
    metric("fan_rpm", "gauge", "Speed of each fan.", rpm_samples)
    speeds = status["fans"]["auto"].get("speeds") or {}
    metric(
        "fan_target_ratio",
        "gauge",
        "Speed that fan control asks of each chosen fan, from 0 to 1.",
        [({"fan": path, "label": labels.get(path, "")}, speed) for path, speed in speeds.items()],
    )
    by_id = {i: drive for drive in drives for i in drive.ids}
    health: list[tuple[dict[str, Any], float]] = []
    warnings: list[tuple[dict[str, Any], float]] = []
    for path, data in smart.items():
        drive = by_id.get(path)
        sample = {"device": path, "drive": drive.name if drive else "", "status": data.get("status", "")}
        health.append((sample, int(data.get("status") == "ok")))
        warnings.append(({"device": path}, len(data.get("warnings", []))))
    metric("smart_ok", "gauge", "1 when the last SMART read found nothing wrong.", health)
    metric("smart_warnings", "gauge", "Number of SMART warnings of the drive.", warnings)
    kinds: dict[str, int] = {}
    for alert in alerts.status() if alerts is not None else []:
        kinds[alert["kind"]] = kinds.get(alert["kind"], 0) + 1
    metric(
        "alerts_active",
        "gauge",
        "Alerts in force, by kind.",
        [({"kind": kind}, count) for kind, count in sorted(kinds.items())],
    )
    return web.Response(
        text="\n".join(lines) + "\n", content_type="text/plain", charset="utf-8", headers={"Cache-Control": "no-store"}
    )


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
    bays: Bays | None = None,
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
    # Without a store (tests), the map lives next to the labels.
    app[BAYS] = bays or Bays(labels.path.parent)
    app[SENSOR_SCAN] = sensor_scan or default_scan
    app[STATE] = {"sensors": SensorReport()}
    app[PASSWORD_CHECKS] = asyncio.Semaphore(1)
    app[LOGINS] = LoginLimiter()
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
    router.add_get("/api/history.csv", get_history_csv)
    router.add_get("/api/smart", get_smart)
    router.add_post("/api/smart", post_smart)
    router.add_get("/api/labels", get_labels)
    router.add_put("/api/labels", put_labels)
    router.add_get("/api/bays", get_bays)
    router.add_put("/api/bays", put_bays)
    router.add_get("/api/cron", get_cron)
    router.add_get("/api/settings", get_settings)
    router.add_put("/api/settings", put_settings)
    router.add_delete("/api/settings", delete_settings)
    router.add_post("/api/restart", post_restart)
    router.add_post("/api/notify/test", post_notify_test)
    router.add_put("/api/alerts/ack", put_alert_ack)
    router.add_put("/api/manual", put_manual)
    router.add_put("/api/manual/pwm", put_manual_pwm)
    router.add_post("/api/pwm-test", post_pwm_test)
    router.add_delete("/api/pwm-test", delete_pwm_test)
    router.add_post("/api/sensors/scan", post_sensor_scan)
    router.add_get("/api/logs", get_logs)
    router.add_get("/metrics", get_metrics)
    router.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
    return app
