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
from .bmc import IPMITOOL_BIN, Bmc
from .const import (
    DEFAULT_CONFIG_DIR,
    HDDFANCONTROL_BIN,
    HDIDLE_BIN,
    HISTORY_DB,
    MODPROBE_BIN,
    PWM_STATE_FILE,
    SENSORS_DETECT_BIN,
    SMARTCTL_BIN,
    SOURCE_ALERTS,
    SOURCE_BMC,
    SOURCE_FAN_CONTROL,
    SOURCE_GATEWAY,
    SOURCE_SENSORS,
    SOURCE_TEMPS,
    SOURCE_WEBUI,
    TEMP_LOG_DIR,
    TEMP_LOG_FILE,
    VERSION,
    env_flag,
)
from .fancontrol import FanController, PwmWriter
from .hardware import Hardware
from .history import History
from .labels import Labels
from .monitor import Monitor
from .privsep import (
    WebUser,
    gateway_command,
    gateway_env,
    given_tls,
    prepare_web_dir,
    secure_socket,
    socket_dir,
    tighten_config,
    web_user,
)
from .sensors import SensorReport, scan_sensor_modules
from .settings import SettingsStore
from .smart import read_smart, start_selftest
from .supervisor import Daemon, LogBuffer, Supervisor
from .templog import TempLog
from .web import STATE, create_app

# Where the package is imported from (root's): the gateway imports it from there only.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------
#   Core
# ---------------------------
@dataclass
class Core:
    """What main() runs: the core API, where it listens, and the gateway in front of it."""

    app: web.Application
    # None when WEBUI_USER cannot be used: no gateway, the core only drives the fans.
    socket_path: Path | None
    user: WebUser | None
    gateway: Daemon | None
    supervisor: Supervisor
    # EXPERIMENTAL: the BMC of a server (iDRAC, iLO), None when BMC=false.
    bmc: Bmc | None = None


# ---------------------------
#   build
# ---------------------------
def build() -> Core:
    """Build the core and its gateway from the environment."""
    config_dir = Path(os.environ.get("CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    if config_dir.is_dir():
        tighten_config(config_dir, logging.getLogger(__name__))
    load_modules = env_flag(os.environ, "LOAD_SENSOR_MODULES", True)

    logs = LogBuffer()
    temp_log_path = config_dir / TEMP_LOG_DIR / TEMP_LOG_FILE
    temp_log = TempLog(temp_log_path)
    # EXPERIMENTAL: the BMC of the host, through /dev/ipmi0; BMC=false leaves it alone.
    bmc = (
        Bmc(os.environ.get("IPMITOOL_BIN", IPMITOOL_BIN), log=lambda text: logs.add(SOURCE_BMC, text))
        if env_flag(os.environ, "BMC", True)
        else None
    )
    hardware = Hardware(bmc=bmc.hwmon if bmc is not None else None)
    fans = FanController(
        PwmWriter(state_file=config_dir / PWM_STATE_FILE, bmc=bmc),
        temp_log.latest,
        lambda text: logs.add(SOURCE_FAN_CONTROL, text),
        hardware.kernel_names,
    )
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

    log = logging.getLogger(__name__)
    user = web_user(env.get("WEBUI_USER"), log)
    socket_path: Path | None = None
    gateway: Daemon | None = None
    if user is not None:
        web_dir = prepare_web_dir(config_dir, user, log)
        cert, key = given_tls(config_dir, user, env.get("TLS_CERT", "").strip(), env.get("TLS_KEY", "").strip(), log)
        socket_path = socket_dir(user) / "core.sock"
        gateway = Daemon(
            SOURCE_GATEWAY,
            sys.executable,
            logs,
            user=(user.uid, user.gid) if user.uid is not None and user.gid is not None else None,
            env=gateway_env(dict(env), socket_path, web_dir, cert, key),
        )
    peers = user.peers if user is not None else frozenset({0})
    app = create_app(
        supervisor,
        hardware,
        temp_log,
        history,
        monitor,
        labels,
        scan,
        Bays(config_dir),
        peers=peers,
        load_modules=load_modules,
        bmc_status=bmc.status if bmc is not None else None,
    )

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        """Scan the sensors, then start everything; stop it all when the server stops."""
        if user is None:
            logs.add(
                SOURCE_WEBUI,
                f"ERROR: hddfancontrol-webui {VERSION}: no usable WEBUI_USER, so no web UI; the fans are controlled",
            )
        else:
            logs.add(SOURCE_WEBUI, f"hddfancontrol-webui {VERSION}: core started, the gateway runs as {user.name}")

        async def startup() -> None:
            # The BMC first: a Dell gets its fans back before anything may take them.
            if bmc is not None:
                try:
                    await bmc.start()
                except Exception as err:  # any failure: report it and go on without it
                    logs.add(SOURCE_BMC, f"ERROR: the BMC cannot be used: {err!r}")
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
    return Core(app, socket_path, user, gateway, supervisor, bmc)


# ---------------------------
#   serve
# ---------------------------
async def serve(core: Core) -> None:
    """Serve the core on its socket and run the gateway, until SIGTERM or SIGINT; then give the fans back and clean up.

    The fans go back first (Supervisor.release_fans), before the gateway
    and the daemons, which may take STOP_TIMEOUT each to stop: Docker
    kills the container when its stop grace period ends. From then on the
    supervisor refuses every change, so a request still coming through the
    gateway cannot take the fans again.
    """
    # Only the gateway connects here: the requests of browsers are logged there (WEBUI_ACCESS_LOG).
    runner = web.AppRunner(core.app, access_log=None, handle_signals=False)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        if core.gateway is not None and core.socket_path is not None and core.user is not None:
            await web.UnixSite(runner, str(core.socket_path)).start()
            secure_socket(core.socket_path, core.user)
            core.gateway.start(gateway_command(PACKAGE_ROOT))
        await stop.wait()
    finally:
        try:
            await core.supervisor.release_fans()
            if core.bmc is not None:
                # The fans of a Dell go back to its iDRAC, whatever RESTORE_FAN_SETTINGS says.
                await core.bmc.close()
        finally:
            if core.gateway is not None:
                await core.gateway.stop()
            await runner.cleanup()
        if core.socket_path is not None:
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
    asyncio.run(serve(core))


if __name__ == "__main__":
    main()
