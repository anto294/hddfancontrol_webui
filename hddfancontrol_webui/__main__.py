"""Start the web UI and the daemons: ``python -m hddfancontrol_webui``.

This is the core, run as root: fan control, the daemons, the monitor and
the API on a Unix socket. It starts the gateway (gateway.py) as WEBUI_USER,
which holds the port and the login and passes the requests it lets through
to that socket (privsep.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from .alerts import Alerts
from .bays import Bays
from .const import (
    DEFAULT_CONFIG_DIR,
    HDDFANCONTROL_BIN,
    HDIDLE_BIN,
    HISTORY_DB,
    MODPROBE_BIN,
    SENSORS_DETECT_BIN,
    SMARTCTL_BIN,
    SOURCE_ALERTS,
    SOURCE_FAN_CONTROL,
    SOURCE_GATEWAY,
    SOURCE_SENSORS,
    SOURCE_TEMPS,
    SOURCE_WEBUI,
    TEMP_LOG_DIR,
    TEMP_LOG_FILE,
    VERSION,
)
from .fancontrol import FanController, PwmWriter
from .hardware import Hardware
from .history import History
from .labels import Labels
from .monitor import Monitor
from .privsep import WebUser, gateway_env, prepare_web_dir, secure_socket, socket_dir, web_user
from .sensors import SensorReport, scan_sensor_modules
from .settings import SettingsStore
from .smart import read_smart, start_selftest
from .supervisor import Daemon, LogBuffer, Supervisor
from .templog import TempLog
from .web import STATE, create_app


# ---------------------------
#   _env_flag
# ---------------------------
def _env_flag(name: str, default: bool) -> bool:
    """Read a true/false environment variable."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


# ---------------------------
#   Core
# ---------------------------
@dataclass
class Core:
    """What main() runs: the core API, where it listens, and the gateway in front of it."""

    app: web.Application
    socket_path: Path
    user: WebUser
    gateway: Daemon


# ---------------------------
#   build
# ---------------------------
def build() -> Core:
    """Build the core and its gateway from the environment."""
    config_dir = Path(os.environ.get("CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    load_modules = _env_flag("LOAD_SENSOR_MODULES", True)

    logs = LogBuffer()
    temp_log_path = config_dir / TEMP_LOG_DIR / TEMP_LOG_FILE
    temp_log = TempLog(temp_log_path)
    hardware = Hardware()
    fans = FanController(PwmWriter(), temp_log.latest, lambda text: logs.add(SOURCE_FAN_CONTROL, text), hardware.kernel_names)
    # The programs are looked up in PATH unless a variable names them: a
    # bare metal installation keeps its own hddfancontrol in its own folder.
    env = os.environ
    labels = Labels(config_dir)
    supervisor = Supervisor(
        SettingsStore(config_dir),
        logs,
        temp_log_path,
        fans,
        hddfancontrol=env.get("HDDFANCONTROL_BIN", HDDFANCONTROL_BIN),
        hdidle=env.get("HDIDLE_BIN", HDIDLE_BIN),
        read_only=hardware.read_only(),
        hardware=hardware,
        on_pwm_moved=labels.move,
    )
    history = History(config_dir / HISTORY_DB)
    smartctl = env.get("SMARTCTL_BIN", SMARTCTL_BIN)

    async def smart_reader(device: str) -> dict[str, Any]:
        return await read_smart(device, smartctl)

    async def selftest(device: str, kind: str) -> dict[str, Any]:
        return await start_selftest(device, kind, smartctl)

    alerts = Alerts(lambda: supervisor.settings, lambda text: logs.add(SOURCE_ALERTS, text), store=history)
    monitor = Monitor(
        history,
        temp_log,
        hardware,
        lambda: supervisor.settings,
        smart_reader,
        lambda text: logs.add(SOURCE_TEMPS, text),
        alerts=alerts,
        status=supervisor.status,
        labels=labels.all,
        selftest=selftest,
    )

    async def scan() -> SensorReport:
        return await scan_sensor_modules(
            lambda text: logs.add(SOURCE_SENSORS, text),
            env.get("SENSORS_DETECT_BIN", SENSORS_DETECT_BIN),
            env.get("MODPROBE_BIN", MODPROBE_BIN),
        )

    user = web_user(env.get("WEBUI_USER"), logging.getLogger(__name__))
    web_dir, cert, key = prepare_web_dir(config_dir, user, env.get("TLS_CERT", "").strip(), env.get("TLS_KEY", "").strip())
    socket_path = socket_dir(user) / "core.sock"
    gateway = Daemon(
        SOURCE_GATEWAY,
        sys.executable,
        logs,
        user=(user.uid, user.gid) if user.uid is not None and user.gid is not None else None,
        env=gateway_env(dict(env), socket_path, web_dir, cert, key),
    )
    app = create_app(supervisor, hardware, temp_log, history, monitor, labels, scan, Bays(config_dir), peers=user.peers)

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        """Scan the sensors, then start everything; stop it all when the server stops."""
        logs.add(SOURCE_WEBUI, f"hddfancontrol-webui {VERSION}: core started, the gateway runs as {user.name}")

        async def startup() -> None:
            # A failed sensor scan must not keep the fans from being controlled.
            if load_modules:
                app[STATE]["sensors"] = SensorReport(state="running")
                try:
                    app[STATE]["sensors"] = await scan()
                except Exception as err:  # any failure: report it and go on
                    logs.add(SOURCE_SENSORS, f"WARNING: the sensor scan failed: {err!r}")
                    app[STATE]["sensors"] = SensorReport(state="done", detect_error=repr(err))
            try:
                await supervisor.start()
            except Exception as err:
                logs.add(SOURCE_WEBUI, f"ERROR: cannot start fan control: {err!r}")
                raise
            finally:
                monitor.start()

        task = asyncio.create_task(startup(), name="startup")
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await monitor.stop()
        await alerts.stop()
        await supervisor.stop()
        history.close()

    app.cleanup_ctx.append(lifecycle)
    return Core(app, socket_path, user, gateway)


# ---------------------------
#   serve
# ---------------------------
async def serve(core: Core, access_log: logging.Logger | None) -> None:
    """Serve the core on its socket and run the gateway, until SIGTERM or SIGINT; then clean up (which gives the fans back).

    The gateway stops first, so no request comes in while the fans are given back.
    """
    runner = web.AppRunner(core.app, access_log=access_log, handle_signals=False)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await web.UnixSite(runner, str(core.socket_path)).start()
        secure_socket(core.socket_path, core.user)
        core.gateway.start(["-m", "hddfancontrol_webui.gateway"])
        await stop.wait()
    finally:
        await core.gateway.stop()
        await runner.cleanup()
        shutil.rmtree(core.socket_path.parent, ignore_errors=True)


# ---------------------------
#   main
# ---------------------------
def main() -> None:
    """Run until SIGTERM or SIGINT, which stop the daemons and give the fans back."""
    logging.basicConfig(level=logging.INFO, format="[webui] %(levelname)s %(name)s: %(message)s")
    # Everything the core writes in /config (history, settings, labels) is root's alone.
    os.umask(0o077)
    core = build()
    # The page polls the API every few seconds: one log line per request
    # would bury everything else. Errors are still logged.
    access_log = logging.getLogger("aiohttp.access") if _env_flag("WEBUI_ACCESS_LOG", False) else None
    asyncio.run(serve(core, access_log))


if __name__ == "__main__":
    main()
