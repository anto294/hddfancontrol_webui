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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .const import (
    HDDFANCONTROL_BIN,
    HDIDLE_BIN,
    LOG_BUFFER_LINES,
    MANUAL_IDLE_TIMEOUT,
    MANUAL_WATCH_PERIOD,
    RESTART_DELAY_MAX,
    RESTART_DELAY_MIN,
    RESTART_RESET_AFTER,
    SOURCE_HDDFANCONTROL,
    SOURCE_HDIDLE,
    SOURCE_PWM_TEST,
    SOURCE_WEBUI,
    STOP_TIMEOUT,
)
from .fancontrol import NULL_HOLD_INTERVALS, SAFETY_HOT, DriveTemps, FanController, NotManual
from .hardware import Hardware
from .settings import SOURCE_FILE, Settings, SettingsError, SettingsStore, hddfancontrol_args, hdidle_args

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
# The PWM test of one output takes a few minutes; past this it is stopped.
PWM_TEST_MAX_S = 600.0
# The temperatures are old, and the PWM test stops, after 3 intervals and at least this.
PWM_TEST_STALE_MIN_S = 120.0
# test.stopped_by when the watch itself failed (the others are the safety reasons of fancontrol: hot, stale).
PWM_TEST_STOPPED_ERROR = "error"


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

    def __init__(
        self,
        name: str,
        binary: str,
        logs: LogBuffer,
        on_exit: Callable[[], None] | None = None,
        user: tuple[int, int] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Name the program for the logs and the API.

        on_exit() runs (in a thread) when the program ended by itself or could
        not start. With user (uid, gid), root starts it as that user, with
        no other group and umask 077; with env, in that environment only.
        """
        self.name = name
        self.binary = binary
        self._user = user
        self._env = env
        self._logs = logs
        self._on_exit = on_exit
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
                extra: dict[str, Any] = {}
                if self._user is not None:
                    extra = {"user": self._user[0], "group": self._user[1], "extra_groups": [], "umask": 0o077}
                proc = await asyncio.create_subprocess_exec(
                    self.binary,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._env,
                    **extra,
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
            if self._on_exit is not None:
                await self._hook(self._on_exit)
            if time.monotonic() - began >= RESTART_RESET_AFTER:
                delay = RESTART_DELAY_MIN
            log(f"Starting again in {delay:g} s")
            await asyncio.sleep(delay)
            self.restarts += 1
            delay = min(delay * 2, RESTART_DELAY_MAX)

    async def _hook(self, hook: Callable[[], None]) -> None:
        """Run a hook in a thread; its failure is logged, never raised."""
        # A stop while the hook writes the fans waits for it, so that
        # nothing is written after the fans were given back.
        future = asyncio.ensure_future(asyncio.to_thread(hook))
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.wait({future})
            raise
        except Exception as err:  # the restart loop must go on
            self._log(f"ERROR: {err!r}")

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
    # Why the watch stopped it: "hot" (a drive at its maximum) or "stale" (no recent temperature).
    stopped_by: str | None = None


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
        hardware: Hardware | None = None,
        on_pwm_moved: Callable[[str, str], None] | None = None,
        fence_samples: Callable[[], None] | None = None,
        manual_timeout: float = MANUAL_IDLE_TIMEOUT,
        manual_watch_period: float = MANUAL_WATCH_PERIOD,
    ) -> None:
        """Use these settings, this log, this fan controller and these programs.

        With read_only, nothing is started and nothing can write the fans:
        the web UI only shows what it can read. With hardware, each fan is
        found again on its chip when the hwmon number changes, and
        on_pwm_moved(old, new) is told (to move its label). fence_samples()
        (TempLog.fence) is called before each start of hddfancontrol, so the
        last sample of the run before is never read with new settings. Manual
        control ends by itself after manual_timeout seconds without
        touch_manual().
        """
        self.read_only = read_only
        self._fence_samples = fence_samples
        self.hardware = hardware
        self._on_pwm_moved = on_pwm_moved
        # Chosen fans whose chip is not on the host now: they are not controlled.
        self.missing_pwms: list[str] = []
        self.store = store
        self.logs = logs
        self.temp_log = temp_log
        self.fans = fans
        self.settings, self.source = store.load()
        self.hddfancontrol = Daemon(SOURCE_HDDFANCONTROL, hddfancontrol, logs, on_exit=self._measuring_lost)
        self.hdidle = Daemon(SOURCE_HDIDLE, hdidle, logs)
        self.manual = False
        self._manual_timeout = manual_timeout
        self._manual_watch_period = manual_watch_period
        self._manual_seen = time.monotonic()
        self._manual_watch: asyncio.Task[None] | None = None
        self.test = PwmTestState()
        self._test_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # Set by release_fans at SIGTERM: nothing starts or changes any more.
        self.closing = False

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
            settings, missing = await self._placed(self.settings)
            if self.source == SOURCE_FILE and settings != self.settings:
                # A fan moved, or got its chip recorded: keep that for the next start.
                # Only a convenience (the next start finds the chip again): a full or
                # read-only /config must never keep the fans from being controlled.
                try:
                    await asyncio.to_thread(self.store.save, settings)
                except OSError as err:
                    self._log(f"ERROR: cannot save the settings with the new place of the fans, they are used anyway: {err}")
            self.settings, self.missing_pwms = settings, missing
            await self._start_daemons()

    async def _placed(self, settings: Settings) -> tuple[Settings, list[str]]:
        """Return settings with each fan at its place now, and the fans whose chip is gone.

        A fan is kept with the chip that owns its PWM file. When the hwmon
        number of that chip changed (at a reboot), the path is corrected and
        the label follows; when the chip is gone, the fan is listed in
        missing_pwms and not controlled, since the old path may now belong
        to another chip.
        """
        pwms, missing = [], []
        for pwm in settings.pwms:
            if self.hardware is None:
                pwms.append(pwm)
                continue
            chip = pwm.chip or await asyncio.to_thread(self.hardware.chip_of, pwm.path)
            if chip is None:
                pwms.append(pwm)
                continue
            where = await asyncio.to_thread(self.hardware.locate, pwm.path, chip)
            if where is None:
                missing.append(pwm.path)
                where = pwm.path
            elif where != pwm.path:
                self._log(f"The fan chip moved: {pwm.path} is now {where}")
                if self._on_pwm_moved is not None:
                    # The label follows the fan; a label that cannot be written never stops a start or an apply.
                    try:
                        await asyncio.to_thread(self._on_pwm_moved, pwm.path, where)
                    except OSError as err:
                        self._log(f"ERROR: cannot move the label of {pwm.path} to {where}: {err}")
            pwms.append(replace(pwm, path=where, chip=chip))
        if self.hardware is not None:
            # A fan that is no PWM output of the host (and not one whose chip is
            # merely gone) is never written: the path came from a file, not from
            # the list of outputs.
            known = await asyncio.to_thread(self.hardware.pwm_paths)
            missing += [pwm.path for pwm in pwms if pwm.path not in known and pwm.path not in missing]
        return replace(settings, pwms=tuple(pwms)), missing

    async def _host_problems(self, settings: Settings) -> list[dict[str, Any]]:
        """What in settings (as _placed returned them) the host contradicts: fans that are no PWM output, drives that are no drive.

        A fan whose chip was recorded but is gone is not one: it comes back
        with its chip, and stays missing until then. A drive path that leads
        nowhere is not one either (an unplugged drive); one that leads to
        something other than a block device is.
        """
        hardware = self.hardware
        if hardware is None:
            return []
        known = await asyncio.to_thread(hardware.pwm_paths)
        problems = [
            {"field": f"pwms.{index}.path", "code": "unknown_pwm", "params": {}}
            for index, pwm in enumerate(settings.pwms)
            if pwm.path not in known and pwm.chip is None
        ]
        for key, drives in (("drives", settings.drives), ("monitor_drives", settings.monitor_drives)):
            for index, drive in enumerate(drives):
                if drive.startswith("/") and await asyncio.to_thread(hardware.is_block_device, drive) is False:
                    problems.append({"field": f"{key}.{index}", "code": "not_block_device", "params": {}})
        return problems

    def _measuring_lost(self) -> None:
        """hddfancontrol ended by itself: the fan controller stops trusting the temperatures until new ones come.

        In manual control too: nothing watches the drives any more, so every
        fan goes to full speed until manual control ends.
        """
        if not self.fans.active:
            return
        if self.manual:
            self._log("WARNING: hddfancontrol stopped, so the drives are not watched: every fan goes to full speed")
        else:
            self._log("WARNING: hddfancontrol stopped, the fans run at full speed until it measures again")
        self.fans.measuring_lost()

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

    async def _start_measuring(self, settings: Settings) -> None:
        """Start hddfancontrol, measuring only, after fencing off the samples of its run before.

        The drives and sensors of a sample are told apart only by their
        number (the sensors come last): an old sample read with other
        settings would make a sensor a drive, or a drive a sensor. Until the
        new run writes, there is no sample, which fan control treats as
        waiting (full speed).
        """
        if self._fence_samples is not None:
            await asyncio.to_thread(self._fence_samples)
        self.hddfancontrol.start(hddfancontrol_args(settings, self._temp_log(), measure_only=True))

    async def _start_daemons(self) -> None:
        """Start what the settings and the mode ask for. Everything must be stopped."""
        if self.closing:
            return
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
                await self._start_measuring(settings)
            else:
                self._log("WARNING: no drive is set, so manual control cannot watch the drive temperatures.")
            # A missing fan's path may belong to another chip now, or to nothing: never written, even for safety.
            self.fans.start_manual(replace(settings, pwms=tuple(p for p in settings.pwms if p.path not in self.missing_pwms)))
            self._log("Manual control: the fans keep the values you set")
            return
        if self.missing_pwms:
            self._log(
                "ERROR: the chip of these fans is not on the host, so they are not controlled: "
                + ", ".join(self.missing_pwms)
                + ". Load its kernel module, or choose the fans again in the web UI."
            )
            return
        if not settings.complete:
            self._log(
                "No drive or no fan is set: the fans are not controlled. "
                "Choose them in the web UI, and use the PWM test to find the values of the fans."
            )
            return
        # hddfancontrol only measures; the fan controller drives the fans in every mode.
        await self._start_measuring(settings)
        self.fans.start_control(settings)

    async def _stop_daemons(self, final: bool = False) -> None:
        """Stop both daemons, then the fan controller, which gives the fans back last (final: the web UI exits)."""
        await asyncio.gather(self.hddfancontrol.stop(), self.hdidle.stop())
        await self.fans.stop(final)

    async def _restart(self) -> None:
        """Stop and start again. The lock is held."""
        await self._stop_daemons()
        await self._start_daemons()

    async def release_fans(self) -> None:
        """Give the fans back at once, before anything else stops: the first thing done at SIGTERM.

        The daemons and the gateway may take up to STOP_TIMEOUT each to
        stop, and Docker kills the container after its stop grace period:
        the fans must not wait for them. hddfancontrol only measures
        (--fan-cmd /bin/true), so nothing else writes them meanwhile. A PWM
        test is cancelled first (its outputs given back), and manual control
        ends; restore_fan_settings is respected as at any final stop. From
        here on nothing starts again: changes answer Busy.
        """
        self.closing = True
        await self.cancel_pwm_test()
        if self._manual_watch is not None:
            await cancel_and_wait(self._manual_watch)
        async with self._lock:
            await self.fans.stop(final=True)
        self._log("Stopping: the fans are given back")

    async def stop(self) -> None:
        """Stop everything, the PWM test too. Used when the container stops."""
        await self.cancel_pwm_test()
        if self._manual_watch is not None:
            await cancel_and_wait(self._manual_watch)
        async with self._lock:
            await self._stop_daemons(final=True)

    async def restart(self) -> None:
        """Restart with the settings in force."""
        async with self._lock:
            self._refuse_during_test()
            await self._restart()

    async def apply(self, settings: Settings) -> None:
        """Save new settings and restart with them."""
        async with self._lock:
            self._refuse_during_test()
            settings, missing = await self._placed(settings)
            if problems := await self._host_problems(settings):
                raise SettingsError(problems)
            await asyncio.to_thread(self.store.save, settings)
            self.settings, self.source, self.missing_pwms = settings, SOURCE_FILE, missing
            self._log("Settings saved, restarting")
            await self._restart()

    async def reset(self) -> None:
        """Go back to the settings of the environment and restart."""
        async with self._lock:
            self._refuse_during_test()
            await asyncio.to_thread(self.store.reset)
            settings, self.source = await asyncio.to_thread(self.store.load)
            self.settings, self.missing_pwms = await self._placed(settings)
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
            if enabled:
                self.touch_manual()
                if self._manual_watch is None or self._manual_watch.done():
                    self._manual_watch = asyncio.create_task(self._watch_manual(), name="manual-watch")
            else:
                self._log("Manual control over, the fans go back to automatic control")
            await self._restart()

    def touch_manual(self) -> None:
        """A web page is open: manual control may go on."""
        self._manual_seen = time.monotonic()

    async def _watch_manual(self) -> None:
        """Leave manual control when no web page has been seen for a while."""
        while self.manual:
            await asyncio.sleep(self._manual_watch_period)
            if self.manual and time.monotonic() - self._manual_seen > self._manual_timeout:
                self._log(f"No web page open for {self._manual_timeout:.0f} s: leaving manual control")
                try:
                    await self.set_manual(False)
                except Busy:
                    # A PWM test runs; try again once it is over.
                    continue

    def set_pwm(self, path: str, value: int) -> None:
        """Set any PWM output of the host in manual control, to find out which fan it drives.

        Each output touched gets its mode and value back when manual control ends.
        """
        if not self.manual:
            raise Busy("Not in manual control")
        self.touch_manual()
        try:
            self.fans.set_manual(path, value)
        except NotManual as err:
            # Manual control is starting (or ending): the controller does not run it right now.
            raise Busy(str(err)) from err

    def _refuse_during_test(self) -> None:
        """Raise Busy while a PWM test runs, or once the web UI is stopping."""
        if self.closing:
            raise Busy("The web UI is stopping")
        if self.test.running:
            raise Busy("A PWM test is running")

    async def start_pwm_test(self, paths: list[str]) -> None:
        """Stop everything and test these PWM outputs one after the other."""
        if self.read_only:
            raise ReadOnly("/sys is read only")
        async with self._lock:
            self._refuse_during_test()
            await self._stop_daemons()
            # hddfancontrol keeps measuring (it never writes a fan this way), so the test can be watched.
            if self.settings.drives:
                await self._start_measuring(self.settings)
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

        task = asyncio.current_task()
        watch = asyncio.create_task(self._watch_test(task, log), name="pwm-test-watch") if task is not None else None
        try:
            for path in paths:
                self.test.current = path
                try:
                    await asyncio.wait_for(self._test_one(path, log), PWM_TEST_MAX_S)
                except TimeoutError:
                    log(f"The test of {path} took longer than {PWM_TEST_MAX_S / 60:g} minutes: stopped")
                    self.test.results[path].update(status="failed", error="timeout")
        except asyncio.CancelledError:
            for result in self.test.results.values():
                if result["status"] == "running":
                    result.update(status="cancelled")
            log("PWM test cancelled")
            raise
        finally:
            if watch is not None:
                try:
                    await asyncio.shield(cancel_and_wait(watch))
                except Exception as err:  # the outputs must be given back whatever the watch did
                    log(f"ERROR: the watch of the PWM test failed: {err!r}")
            await asyncio.shield(self._after_test(log))

    async def _watch_test(self, task: asyncio.Task[Any], log: Callable[[str], None]) -> None:
        """Cancel the PWM test when a drive reaches its maximum or the temperatures stop coming in.

        The test stops and starts the fans for minutes. hddfancontrol keeps
        measuring meanwhile, and this reads its samples every interval, with
        the checks of manual control. Without drives there is nothing to
        watch: only the time limit of each output holds.
        """
        settings = self.settings
        if not settings.drives:
            log("WARNING: no drive is set, so the drive temperatures are not watched during the test")
            return
        interval = float(settings.interval_s)
        temps = DriveTemps(NULL_HOLD_INTERVALS * interval)
        started = datetime.now(UTC)
        stale_after = max(3 * interval, PWM_TEST_STALE_MIN_S)
        while True:
            await asyncio.sleep(interval)
            try:
                reason, hottest = await self.fans.watch_drives(settings, temps, started, stale_after)
            except Exception as err:  # when in doubt the test stops: the fans go back to their control
                log(f"ERROR: the drive temperatures cannot be watched ({err!r}): the PWM test stops")
                self.test.stopped_by = PWM_TEST_STOPPED_ERROR
                task.cancel()
                return
            if reason is None:
                continue
            if reason == SAFETY_HOT:
                log(f"WARNING: a drive is at {hottest:g} °C: the PWM test stops, the fans go back to their control")
            else:
                log("WARNING: no recent drive temperature: the PWM test stops, the fans go back to their control")
            self.test.stopped_by = reason
            task.cancel()
            return

    async def _after_test(self, log: Callable[[str], None]) -> None:
        """Give the outputs back and start everything again, holding the lock.

        Until the lock is held the test still counts as running, so a
        restart or new settings cannot start the daemons at the same time.
        """
        async with self._lock:
            # hddfancontrol pwm-test leaves the outputs in manual mode at its
            # last value: give each one back the mode and value it had.
            await asyncio.to_thread(self.fans.writer.restore)
            await self.hddfancontrol.stop()  # it measured for the watch; _start_daemons starts it again
            self.test.running = False
            self.test.current = None
            self.test.finished_at = datetime.now(UTC).isoformat()
            log("PWM test over, starting again")
            await self._start_daemons()

    def status(self) -> dict[str, Any]:
        """Return the state of everything as JSON-ready data."""
        test = self.test
        return {
            "mode": self.mode,
            "read_only": self.read_only,
            "missing_pwms": self.missing_pwms,
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
                "stopped_by": test.stopped_by,
            },
        }
