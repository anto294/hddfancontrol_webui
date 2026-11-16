"""Run hddfancontrol, hd-idle and the PWM test, and keep their output.

The supervisor replaces start_services.sh of the upstream image. The
differences that matter:

- A daemon that exits is started again after a delay, instead of the whole
  container stopping. The delay grows while it keeps failing.
- New settings restart the daemons without restarting the container.
- Without drives or fans, nothing runs and the web UI says so, as the
  upstream script waited for a shell to run pwmconfig.
- The PWM test stops the daemons, which would fight it for the fans, and
  starts them again when it ends, however it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .const import (
    HDDFANCONTROL_BIN,
    HDIDLE_BIN,
    LOG_BUFFER_LINES,
    RESTART_DELAY_MAX,
    RESTART_DELAY_MIN,
    RESTART_RESET_AFTER,
    SOURCE_HDDFANCONTROL,
    SOURCE_HDIDLE,
    SOURCE_PWM_TEST,
    SOURCE_WEBUI,
    STOP_TIMEOUT,
)
from .fancontrol import FanController
from .settings import SOURCE_FILE, Settings, SettingsStore, hddfancontrol_args, hdidle_args

_THRESHOLDS = re.compile(r"start/stop thresholds: (\d+)/(\d+)")
_TEST_FAILED = re.compile(r"test failed: (.*)$")

MODE_RUNNING = "running"
MODE_WAITING = "waiting"
MODE_PWM_TEST = "pwm_test"
MODE_STOPPED = "stopped"
MODE_MANUAL = "manual"
MODE_READ_ONLY = "read_only"


# ---------------------------
#   Busy
# ---------------------------
class Busy(RuntimeError):
    """The request cannot run now, because a PWM test is running."""


# ---------------------------
#   ReadOnly
# ---------------------------
class ReadOnly(RuntimeError):
    """The fans cannot be written: /sys is read only (a container that is not privileged)."""


# ---------------------------
class NotSelected(ValueError):
    """The PWM output is not one of the fans chosen in the settings."""


# ---------------------------
#   LogBuffer
# ---------------------------
class LogBuffer:
    """The last lines of every process, numbered so a client asks only for new ones.

    Every line also goes to stdout with its source, so docker logs keeps
    showing what the upstream image showed.
    """

    def __init__(self, maxlen: int = LOG_BUFFER_LINES, echo: bool = True) -> None:
        """Keep at most maxlen lines."""
        self._lines: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._echo = echo

    @property
    def last_seq(self) -> int:
        """Number of the newest line, 0 before the first."""
        return self._seq

    def add(self, source: str, text: str) -> None:
        """Add a line."""
        self._seq += 1
        text = text.rstrip()
        self._lines.append({"seq": self._seq, "time": datetime.now(UTC).isoformat(), "source": source, "text": text})
        if self._echo:
            print(f"[{source}] {text}", file=sys.stdout, flush=True)

    def since(self, seq: int) -> list[dict[str, Any]]:
        """Return the lines newer than seq that are still kept."""
        return [line for line in self._lines if line["seq"] > seq]


# ---------------------------
#   _terminate
# ---------------------------
async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Ask a process to stop, and kill it if it does not in time."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


# ---------------------------
#   _pump
# ---------------------------
async def _pump(proc: asyncio.subprocess.Process, log: Callable[[str], None]) -> None:
    """Pass every line of the output of a process to log, until it closes."""
    if proc.stdout is None:
        return
    while True:
        try:
            raw = await proc.stdout.readline()
        except ValueError:
            # A line longer than the buffer: skip it rather than stop reading.
            log("(line too long, skipped)")
            continue
        if not raw:
            return
        log(raw.decode("utf-8", errors="replace"))


# ---------------------------
#   Daemon
# ---------------------------
class Daemon:
    """One long running program, started again when it exits."""

    def __init__(self, name: str, binary: str, logs: LogBuffer) -> None:
        """Name the program for the logs and the API."""
        self.name = name
        self.binary = binary
        self._logs = logs
        self._task: asyncio.Task[None] | None = None
        self.state = MODE_STOPPED
        self.pid: int | None = None
        self.started_at: str | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.args: list[str] = []

    @property
    def active(self) -> bool:
        """Whether the program runs or waits to be started again."""
        return self._task is not None and not self._task.done()

    def start(self, args: list[str]) -> None:
        """Start the program with these arguments. It must not be active."""
        self.args = args
        self.restarts = 0
        self.last_exit = None
        self._task = asyncio.create_task(self._run(args), name=f"daemon-{self.name}")

    async def stop(self) -> None:
        """Stop the program and wait until it has exited."""
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_wait(task)
        self.state = MODE_STOPPED
        self.pid = None

    async def _run(self, args: list[str]) -> None:
        """Run the program, and run it again after a delay whenever it exits."""
        delay = RESTART_DELAY_MIN
        log = self._log
        while True:
            began = time.monotonic()
            log(f"Starting: {self.binary} {' '.join(args)}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
            except OSError as err:
                log(f"Cannot start {self.binary}: {err}")
                self.state = "failed"
            else:
                self.state = MODE_RUNNING
                self.pid = proc.pid
                self.started_at = datetime.now(UTC).isoformat()
                try:
                    await _pump(proc, log)
                    self.last_exit = await proc.wait()
                finally:
                    # Cancelled by stop(): the program must not outlive it.
                    await asyncio.shield(_terminate(proc))
                    self.pid = None
                log(f"Exited with code {self.last_exit}")
                self.state = "restarting"
            if time.monotonic() - began >= RESTART_RESET_AFTER:
                delay = RESTART_DELAY_MIN
            log(f"Starting again in {delay:g} s")
            await asyncio.sleep(delay)
            self.restarts += 1
            delay = min(delay * 2, RESTART_DELAY_MAX)

    def _log(self, text: str) -> None:
        """Log a line under the name of this program."""
        self._logs.add(self.name, text)

    def status(self) -> dict[str, Any]:
        """Return the state of the program as JSON-ready data."""
        return {
            "state": self.state,
            "pid": self.pid,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "args": self.args,
        }


# ---------------------------
#   PwmTestState
# ---------------------------
@dataclass
class PwmTestState:
    """What the last PWM test found."""

    running: bool = False
    paths: list[str] = field(default_factory=list)
    current: str | None = None
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None


# ---------------------------
#   Supervisor
# ---------------------------
class Supervisor:
    """Start, stop and restart the daemons and the fan controller with the settings in force."""

    def __init__(
        self,
        store: SettingsStore,
        logs: LogBuffer,
        temp_log: Path | None,
        fans: FanController,
        hddfancontrol: str = HDDFANCONTROL_BIN,
        hdidle: str = HDIDLE_BIN,
        read_only: bool = False,
    ) -> None:
        """Use these settings, this log, this fan controller and these programs.

        With read_only, nothing is started and nothing can write the fans:
        the web UI only shows what it can read.
        """
        self.read_only = read_only
        self.store = store
        self.logs = logs
        self.temp_log = temp_log
        self.fans = fans
        self.settings, self.source = store.load()
        self.hddfancontrol = Daemon(SOURCE_HDDFANCONTROL, hddfancontrol, logs)
        self.hdidle = Daemon(SOURCE_HDIDLE, hdidle, logs)
        self.manual = False
        self.test = PwmTestState()
        self._test_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        """What drives the fans now."""
        if self.read_only:
            return MODE_READ_ONLY
        if self.test.running:
            return MODE_PWM_TEST
        if self.manual:
            return MODE_MANUAL
        if self.hddfancontrol.active or self.fans.active:
            return MODE_RUNNING
        if not self.settings.complete:
            return MODE_WAITING
        return MODE_STOPPED

    def _log(self, text: str) -> None:
        """Log a line of the supervisor itself."""
        self.logs.add(SOURCE_WEBUI, text)

    async def start(self) -> None:
        """Start everything with the settings in force."""
        async with self._lock:
            await self._start_daemons()

    def _temp_log(self) -> Path | None:
        """The temperature log path, if its directory can be created."""
        if self.temp_log is None:
            return None
        try:
            self.temp_log.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self._log(f"Cannot create {self.temp_log.parent}, temperatures are not recorded: {err}")
            return None
        return self.temp_log

    async def _start_daemons(self) -> None:
        """Start what the settings and the mode ask for. Everything must be stopped."""
        if self.read_only:
            self._log(
                "WARNING: /sys is read only (the container is not privileged), so the fans cannot be controlled: "
                "hddfancontrol and hd-idle do not run and the web UI only shows. Run the container with privileged: true."
            )
            return
        settings = self.settings
        hdidle = hdidle_args(settings)
        if hdidle is None:
            self._log("hd-idle does not run: no drive has a spin down time.")
        else:
            self.hdidle.start(hdidle)
        if self.manual:
            if settings.drives:
                self.hddfancontrol.start(hddfancontrol_args(settings, self._temp_log(), measure_only=True))
            else:
                self._log("WARNING: no drive is set, so manual control cannot watch the drive temperatures.")
            self.fans.start_manual(settings)
            self._log("Manual control: the fans keep the values you set")
            return
        if not settings.complete:
            self._log(
                "No drive or no fan is set: the fans are not controlled. "
                "Choose them in the web UI, and use the PWM test to find the values of the fans."
            )
            return
        auto = settings.control_mode == "auto"
        self.hddfancontrol.start(hddfancontrol_args(settings, self._temp_log(), measure_only=auto))
        if auto:
            self.fans.start_auto(settings)

    async def _stop_daemons(self) -> None:
        """Stop the fan controller, which gives the fans back, and both daemons."""
        await self.fans.stop()
        await asyncio.gather(self.hddfancontrol.stop(), self.hdidle.stop())

    async def _restart(self) -> None:
        """Stop and start again. The lock is held."""
        await self._stop_daemons()
        await self._start_daemons()

    async def stop(self) -> None:
        """Stop everything, the PWM test too. Used when the container stops."""
        await self.cancel_pwm_test()
        async with self._lock:
            await self._stop_daemons()

    async def restart(self) -> None:
        """Restart with the settings in force."""
        async with self._lock:
            self._refuse_during_test()
            await self._restart()

    async def apply(self, settings: Settings) -> None:
        """Save new settings and restart with them."""
        async with self._lock:
            self._refuse_during_test()
            await asyncio.to_thread(self.store.save, settings)
            self.settings, self.source = settings, SOURCE_FILE
            self._log("Settings saved, restarting")
            await self._restart()

    async def reset(self) -> None:
        """Go back to the settings of the environment and restart."""
        async with self._lock:
            self._refuse_during_test()
            await asyncio.to_thread(self.store.reset)
            self.settings, self.source = await asyncio.to_thread(self.store.load)
            self._log("Saved settings deleted, using the environment variables")
            await self._restart()

    async def set_manual(self, enabled: bool) -> None:
        """Enter or leave manual control. It is never saved: a restart leaves it."""
        async with self._lock:
            self._refuse_during_test()
            if enabled == self.manual:
                return
            if enabled and self.read_only:
                raise ReadOnly("/sys is read only")
            self.manual = enabled
            if not enabled:
                self._log("Manual control over, the fans go back to automatic control")
            await self._restart()

    def set_pwm(self, path: str, value: int) -> None:
        """Set any PWM output of the host in manual control, to find out which fan it drives.

        Each output touched gets its mode and value back when manual control ends.
        """
        if not self.manual:
            raise Busy("Not in manual control")
        self.fans.set_manual(path, value)

    def _refuse_during_test(self) -> None:
        """Raise Busy while a PWM test runs."""
        if self.test.running:
            raise Busy("A PWM test is running")

    async def start_pwm_test(self, paths: list[str]) -> None:
        """Stop everything and test these PWM outputs one after the other."""
        if self.read_only:
            raise ReadOnly("/sys is read only")
        async with self._lock:
            self._refuse_during_test()
            await self._stop_daemons()
            self.test = PwmTestState(running=True, paths=list(paths), started_at=datetime.now(UTC).isoformat())
            self._test_task = asyncio.create_task(self._run_pwm_test(list(paths)), name="pwm-test")

    async def cancel_pwm_test(self) -> None:
        """Stop the PWM test, if one runs. Everything starts again."""
        task = self._test_task
        if task is not None and not task.done():
            await cancel_and_wait(task)

    async def _test_one(self, path: str, log: Callable[[str], None]) -> None:
        """Run 'hddfancontrol pwm-test' on one PWM output and keep its result."""
        log(f"Testing {path}. The fan stops and starts several times; this takes a few minutes.")
        result: dict[str, Any] = {"status": "running"}
        self.test.results[path] = result
        try:
            await asyncio.to_thread(self.fans.writer.take, path)
        except OSError as err:
            log(f"Cannot put {path} in manual mode: {err}")
            result.update(status="failed", error=str(err))
            return

        def watch(text: str) -> None:
            log(text)
            if match := _THRESHOLDS.search(text):
                result.update(status="done", start=int(match.group(1)), stop=int(match.group(2)))
            elif match := _TEST_FAILED.search(text):
                result.update(status="failed", error=match.group(1))

        try:
            proc = await asyncio.create_subprocess_exec(
                self.hddfancontrol.binary,
                "-v",
                "INFO",
                "pwm-test",
                "--pwm",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            log(f"Cannot start {self.hddfancontrol.binary}: {err}")
            result.update(status="failed", error=str(err))
            return
        try:
            await _pump(proc, watch)
            code = await proc.wait()
        finally:
            await asyncio.shield(_terminate(proc))
        if result["status"] == "running":
            result.update(status="failed", error=f"exit code {code}")

    async def _run_pwm_test(self, paths: list[str]) -> None:
        """Test each path, then start everything again however the test ends."""

        def log(text: str) -> None:
            self.logs.add(SOURCE_PWM_TEST, text)

        try:
            for path in paths:
                self.test.current = path
                await self._test_one(path, log)
        except asyncio.CancelledError:
            for result in self.test.results.values():
                if result["status"] == "running":
                    result.update(status="cancelled")
            log("PWM test cancelled")
            raise
        finally:
            # hddfancontrol pwm-test leaves the outputs in manual mode at its
            # last value: give each one back the mode and value it had.
            await asyncio.shield(asyncio.to_thread(self.fans.writer.restore))
            self.test.running = False
            self.test.current = None
            self.test.finished_at = datetime.now(UTC).isoformat()
            log("PWM test over, starting again")
            await asyncio.shield(self._start_daemons())

    def status(self) -> dict[str, Any]:
        """Return the state of everything as JSON-ready data."""
        test = self.test
        return {
            "mode": self.mode,
            "read_only": self.read_only,
            "control_mode": self.settings.control_mode,
            "manual": self.manual,
            "settings_source": self.source,
            "settings_error": None if self.store.error is None else self.store.error.problems,
            "daemons": {
                SOURCE_HDDFANCONTROL: self.hddfancontrol.status(),
                SOURCE_HDIDLE: self.hdidle.status(),
            },
            "fans": self.fans.status(),
            "pwm_test": {
                "running": test.running,
                "paths": test.paths,
                "current": test.current,
                "results": test.results,
                "started_at": test.started_at,
                "finished_at": test.finished_at,
            },
        }
