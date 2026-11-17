"""Start the web UI and the daemons: ``python -m hddfancontrol_webui``."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiohttp import web

from .alerts import Alerts
from .auth import AuthStore
from .bays import Bays
from .const import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_PORT,
    HDDFANCONTROL_BIN,
    HDIDLE_BIN,
    HISTORY_DB,
    MODPROBE_BIN,
    SENSORS_DETECT_BIN,
    SMARTCTL_BIN,
    SOURCE_ALERTS,
    SOURCE_FAN_CONTROL,
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
from .sensors import SensorReport, scan_sensor_modules
from .settings import SettingsStore
from .smart import read_smart, start_selftest
from .supervisor import LogBuffer, Supervisor
from .templog import TempLog
from .tls import DualListener, TlsError, ssl_context
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
#   build
# ---------------------------
def build() -> tuple[web.Application, int, ssl.SSLContext | None]:
    """Build the application from the environment, and return it with its port and its TLS context (or None).

    A certificate that cannot be read or made leaves HTTPS off, with an
    error in the log, instead of stopping: the fans come first.
    """
    config_dir = Path(os.environ.get("CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    port = int(os.environ.get("WEBUI_PORT", str(DEFAULT_PORT)))
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
    auth = AuthStore(config_dir)

    async def scan() -> SensorReport:
        return await scan_sensor_modules(
            lambda text: logs.add(SOURCE_SENSORS, text),
            env.get("SENSORS_DETECT_BIN", SENSORS_DETECT_BIN),
            env.get("MODPROBE_BIN", MODPROBE_BIN),
        )

    context: ssl.SSLContext | None = None
    tls_error = ""
    if _env_flag("ENABLE_TLS", False):
        try:
            context = ssl_context(
                config_dir, env.get("TLS_CERT", "").strip(), env.get("TLS_KEY", "").strip(), env.get("OPENSSL_BIN", "openssl")
            )
        except TlsError as err:
            tls_error = str(err)
    # Redirecting to an HTTPS that failed would lock everyone out.
    force_https = _env_flag("FORCE_HTTPS", False) and not tls_error
    app = create_app(
        supervisor, hardware, temp_log, history, monitor, auth, labels, scan, Bays(config_dir), force_https=force_https
    )

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        """Scan the sensors, then start everything; stop it all when the server stops."""
        scheme = "HTTPS and HTTP" if context else "HTTP"
        redirect = ", HTTP redirected to HTTPS" if force_https else ""
        logs.add(SOURCE_WEBUI, f"hddfancontrol-webui {VERSION}, listening on port {port} ({scheme}{redirect})")
        if tls_error:
            logs.add(SOURCE_WEBUI, f"ERROR: HTTPS is off: {tls_error}")
        if not auth.enabled:
            logs.add(
                SOURCE_WEBUI,
                "WARNING: no username and password are set, anyone who reaches the port can change the fans. "
                "Set them in the web UI (Security) or with WEBUI_USERNAME and WEBUI_PASSWORD.",
            )

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
    return app, port, context


# ---------------------------
#   serve
# ---------------------------
async def serve(
    app: web.Application, host: str, port: int, context: ssl.SSLContext | None, access_log: logging.Logger | None
) -> None:
    """Serve until SIGTERM or SIGINT, then clean up (which gives the fans back).

    With a TLS context, one port answers both HTTPS and HTTP (DualListener).
    """
    runner = web.AppRunner(app, access_log=access_log, handle_signals=False)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    listener: DualListener | None = None
    try:
        if context is None:
            await web.TCPSite(runner, host, port).start()
        else:
            server = runner.server
            if server is None:  # setup() made it
                raise RuntimeError("the server was not made")
            listener = DualListener(server, host, port, context)
            await listener.start()
        await stop.wait()
    finally:
        if listener is not None:
            await listener.close()
        await runner.cleanup()


# ---------------------------
#   main
# ---------------------------
def main() -> None:
    """Run until SIGTERM or SIGINT, which stop the daemons and give the fans back."""
    logging.basicConfig(level=logging.INFO, format="[webui] %(levelname)s %(name)s: %(message)s")
    app, port, context = build()
    # A container listens on every interface; Docker decides what is published.
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")  # noqa: S104  # nosec B104
    # The page polls the API every few seconds: one log line per request
    # would bury everything else. Errors are still logged.
    access_log = logging.getLogger("aiohttp.access") if _env_flag("WEBUI_ACCESS_LOG", False) else None
    asyncio.run(serve(app, host, port, context, access_log))


if __name__ == "__main__":
    main()
