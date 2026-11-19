"""The core API of the web UI: JSON over a Unix socket, for the gateway only.

This process runs as root (it writes the PWM files and talks to the
drives), often in a privileged container, so the network never reaches it:
gateway.py, run without privileges, holds the port, TLS, the login, the
CSRF check and the static files, and passes on only the requests it let
through, over a Unix socket that only its user can open. core_guard checks
that user again on every connection (SO_PEERCRED), and logs every change in
the audit log with the user and client address the gateway sends.

Every value is checked in settings.py; a PWM test or a manual fan value only
goes to PWM files that the host really has.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from .bays import BayError, Bays
from .bmc import Bmc, BmcConfigError
from .const import BMC_PWM, HEADER_CLIENT, HEADER_USER, HISTORY_MAX_HOURS, SOURCE_AUDIT, SOURCE_SENSORS, VERSION
from .cron import CronError, parse, windows
from .hardware import Hardware
from .history import History
from .httputil import json_error as _json_error
from .httputil import read_json as _read_json
from .labels import LabelError, Labels
from .monitor import Monitor
from .notify import NotifyError
from .sensors import SensorReport, scan_sensor_modules
from .settings import SettingsError, from_dict
from .supervisor import Busy, ReadOnly, Supervisor
from .templog import TempLog

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
SensorScan = Callable[[], Awaitable[SensorReport]]

SUPERVISOR: web.AppKey[Supervisor] = web.AppKey("supervisor", Supervisor)
HARDWARE: web.AppKey[Hardware] = web.AppKey("hardware", Hardware)
TEMP_LOG: web.AppKey[TempLog] = web.AppKey("temp_log", TempLog)
HISTORY: web.AppKey[History] = web.AppKey("history", History)
MONITOR: web.AppKey[Monitor] = web.AppKey("monitor", Monitor)
LABELS: web.AppKey[Labels] = web.AppKey("labels", Labels)
BAYS: web.AppKey[Bays] = web.AppKey("bays", Bays)
SENSOR_SCAN: web.AppKey[SensorScan] = web.AppKey("sensor_scan")
STATE: web.AppKey[dict[str, Any]] = web.AppKey("state", dict)
PEERS: web.AppKey[frozenset[int] | None] = web.AppKey("peers")
AUDIT: web.AppKey[AuditLog] = web.AppKey("audit")
LOAD_MODULES: web.AppKey[bool] = web.AppKey("load_modules", bool)
# EXPERIMENTAL: the BMC of a server (bmc.Bmc), or None without one (BMC=false, tests).
BMC: web.AppKey[Bmc | None] = web.AppKey("bmc")

# The same change sent again within this many seconds (a slider, a script)
# is counted instead of logged again.
AUDIT_REPEAT_S = 60.0


# ---------------------------
#   AuditLog
# ---------------------------
class AuditLog:
    """The audit lines, with a run of the same line counted: the line, then how many more came in its minute.

    The log buffer is shared with the daemons and the alerts: a manual fan
    slider sends a request at every stop, and must not push those out. The
    window is fixed (AUDIT_REPEAT_S from the logged line), and the count is
    logged when it ends, so a steady stream still shows once a minute.
    """

    def __init__(self, add: Callable[[str], None], clock: Callable[[], float] = time.monotonic) -> None:
        """Write the lines with add; clock gives the time in seconds."""
        self._add = add
        self._clock = clock
        self._last = ""
        self._time = 0.0
        self._repeats = 0
        self._timer: asyncio.TimerHandle | None = None

    def write(self, line: str) -> None:
        """Log line, or count it when it is the last one again within AUDIT_REPEAT_S of when that was logged."""
        now = self._clock()
        if line == self._last and now - self._time < AUDIT_REPEAT_S:
            self._repeats += 1
            if self._timer is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:  # no loop (a test): the count waits for the next line or flush
                    return
                self._timer = loop.call_later(AUDIT_REPEAT_S - (now - self._time), self.flush)
            return
        self.flush()
        self._add(line)
        self._last, self._time = line, now

    def flush(self) -> None:
        """Log how many times the last line came again, if it did; the next one is logged afresh."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._repeats:
            self._add(f"{self._last} (and {self._repeats} more like it)")
            self._repeats = 0
        self._last = ""


# ---------------------------
#   _peer_uid
# ---------------------------
def _peer_uid(request: web.Request) -> int | None:
    """The user id of the process at the other end of a Unix socket, or None (not a Unix socket)."""
    transport = request.transport
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None or sock.family != socket.AF_UNIX:
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


# ---------------------------
#   core_guard
# ---------------------------
@web.middleware
async def core_guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Answer only the users of PEERS (the gateway, and root), and log every change in the audit log.

    The socket's mode already keeps other users out; this checks the peer
    of each connection again, in case the file mode is ever loosened.
    """
    peers = request.app[PEERS]
    if peers is not None and _peer_uid(request) not in peers:
        return _json_error(403, "forbidden")
    if request.method in ("GET", "HEAD"):
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _audit(request, exc.status)
        raise
    _audit(request, response.status)
    return response


# ---------------------------
#   _audit
# ---------------------------
def _audit(request: web.Request, status: int) -> None:
    """One line of the audit log: what changed, by whom, from where, and how it ended."""
    user = request.headers.get(HEADER_USER) or "?"
    client = request.headers.get(HEADER_CLIENT) or "?"
    # The raw path keeps %0A encoded: a client cannot add lines of its own to the log.
    path = request.rel_url.raw_path
    request.app[AUDIT].write(f"{request.method} {path} by {user!r} from {client}: {status}")


# ---------------------------
#   healthz
# ---------------------------
async def healthz(request: web.Request) -> web.Response:
    """Answer the gateway's health check: the event loop of the core is alive."""
    return web.Response(text="ok")


# ---------------------------
#   get_status
# ---------------------------
async def get_status(request: web.Request) -> web.Response:
    """State of the daemons, the fans, the PWM test and the sensor modules; never the login state (GET /api/auth has it)."""
    app = request.app
    # The pages ask for the status every few seconds while they are open.
    app[SUPERVISOR].touch_manual()
    data = app[SUPERVISOR].status()
    alerts = app[MONITOR].alerts
    report: SensorReport = app[STATE]["sensors"]
    bmc = app[BMC]
    data.update(
        version=VERSION,
        sensors=report.to_dict(),
        latest=await asyncio.to_thread(app[TEMP_LOG].latest),
        alerts=alerts.status() if alerts is not None else [],
        bmc=bmc.status() if bmc is not None else None,
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
#   _csv_cell
# ---------------------------
def _csv_cell(value: Any) -> Any:
    """A cell of the CSV: text that a spreadsheet would read as a formula gets a quote in front.

    Names and labels come from the host and the users: "=HYPERLINK(...)"
    must open as text, not run. Numbers are left as they are.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ---------------------------
#   _history_csv
# ---------------------------
def _history_csv(data: dict[str, Any], labels: dict[str, str]) -> str:
    """The rows of the history as CSV text: time (UTC), kind, name, label, value. Blocking (CPU)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time_utc", "kind", "name", "label", "value"])
    for series in data["series"]:
        label = labels.get(series["name"], "")
        for ts, value in series["points"]:
            when = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            writer.writerow([_csv_cell(cell) for cell in (when, series["kind"], series["name"], label, value)])
    return buffer.getvalue()


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
    # Up to CSV_MAX_POINTS rows per series: built in a thread, never on the event loop.
    text = await asyncio.to_thread(_history_csv, data, request.app[LABELS].all())
    name = f"hddfancontrol-history-{hours:g}h.csv"
    return web.Response(
        text=text,
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
#   get_bmc
# ---------------------------
async def get_bmc(request: web.Request) -> web.Response:
    """EXPERIMENTAL: the IPMI tab: the configuration of the BMC, the server found, the models and CPU profiles."""
    bmc = request.app[BMC]
    data = bmc.describe() if bmc is not None else None
    if data is None:
        return _json_error(404, "bmc_missing")
    return web.json_response(data)


# ---------------------------
#   put_bmc
# ---------------------------
async def put_bmc(request: web.Request) -> web.Response:
    """EXPERIMENTAL: store who drives the fans of the BMC and how. Fan control is not restarted."""
    bmc = request.app[BMC]
    if bmc is None or not bmc.present:
        return _json_error(404, "bmc_missing")
    data = await _read_json(request)
    try:
        config = await asyncio.to_thread(bmc.set_config, data)
    except BmcConfigError as err:
        return _json_error(400, err.problems[0]["code"], err.problems)
    except OSError as err:
        _LOGGER.error("Cannot save the BMC configuration: %s", err)
        return _json_error(500, "save_failed")
    return web.json_response(config.to_dict())


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
    except SettingsError as err:  # a fan that is no PWM output of the host
        return _json_error(400, "invalid_settings", err.problems)
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
    if BMC_PWM in paths:
        # The fans of a BMC never stop: there is no start and stop value to find.
        return _json_error(400, "bmc_no_test")
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
    """Scan for the sensor modules again, after loading them on the host for example.

    With LOAD_SENSOR_MODULES=false no scan ever runs: sensors-detect and
    modprobe change the host, which whoever set it does not want.
    """
    if not request.app[LOAD_MODULES]:
        return _json_error(409, "scan_disabled")
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
    labels: Labels,
    sensor_scan: SensorScan | None = None,
    bays: Bays | None = None,
    peers: frozenset[int] | None = None,
    load_modules: bool = True,
    bmc: Bmc | None = None,
) -> web.Application:
    """Build the core API around parts that the caller starts and stops.

    peers are the user ids that may connect (None: anyone, as in the tests
    over TCP); the gateway's and root's in production. load_modules is
    LOAD_SENSOR_MODULES: without it, POST /api/sensors/scan is refused.
    """

    async def default_scan() -> SensorReport:
        return await scan_sensor_modules(lambda text: supervisor.logs.add(SOURCE_SENSORS, text))

    app = web.Application(middlewares=[core_guard], client_max_size=64 * 1024)
    app[SUPERVISOR] = supervisor
    app[PEERS] = peers
    app[AUDIT] = AuditLog(lambda text: supervisor.logs.add(SOURCE_AUDIT, text))

    async def flush_audit(app: web.Application) -> None:
        """Log the count of the last audit line before the core stops."""
        app[AUDIT].flush()

    app.on_cleanup.append(flush_audit)
    app[HARDWARE] = hardware
    app[BMC] = bmc
    app[TEMP_LOG] = temp_log
    app[HISTORY] = history
    app[MONITOR] = monitor
    app[LABELS] = labels
    # Without a store (tests), the map lives next to the labels.
    app[BAYS] = bays or Bays(labels.path.parent)
    app[SENSOR_SCAN] = sensor_scan or default_scan
    app[LOAD_MODULES] = load_modules
    app[STATE] = {"sensors": SensorReport()}
    router = app.router
    router.add_get("/healthz", healthz)
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
    router.add_get("/api/bmc", get_bmc)
    router.add_put("/api/bmc", put_bmc)
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
    return app
