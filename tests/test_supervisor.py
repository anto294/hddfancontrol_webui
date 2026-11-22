"""Running, restarting and stopping the daemons, the modes and the PWM test."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptMaker

from hddfancontrol_webui import supervisor as supervisor_module
from hddfancontrol_webui.fancontrol import FanController, PwmWriter
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.settings import SettingsError, SettingsStore, from_dict
from hddfancontrol_webui.supervisor import Busy, Daemon, LogBuffer, ReadOnly, Supervisor
from hddfancontrol_webui.templog import TempLog

PWM1 = "/sys/class/hwmon/hwmon2/pwm1"
CONFIG = {
    "drives": ["/dev/disk/by-id/ata-X"],
    "pwms": [{"path": PWM1, "start": 200, "stop": 75}],
    "interval_s": 1,
}

# Stands in for hddfancontrol: records its arguments, answers pwm-test, runs until stopped.
FAKE_HDDFANCONTROL = """
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\\n")
if "pwm-test" in sys.argv:
    path = sys.argv[-1]
    fake_sys = os.environ.get("FAKE_SYS")
    if fake_sys and log:
        with open(fake_sys + path.removeprefix("/sys") + "_enable") as enable, open(log, "a") as handle:
            handle.write(f"enable during test: {enable.read().strip()}\\n")
    print(f"INFO Testing fan {path}, this may take a long time", flush=True)
    if path.endswith("pwm2"):
        print(f"ERROR Fan {path} test failed: no RPM change", flush=True)
    else:
        print(f"INFO Fan {path}] start/stop thresholds: 180/62", flush=True)
    sys.exit(0)
print("running", flush=True)
time.sleep(60)
"""


def read(path: Path) -> str:
    """Read a file that may not exist yet."""
    return path.read_text() if path.exists() else ""


async def wait_for(predicate: Any, limit: float = 5.0) -> None:
    """Wait until predicate() is true."""
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
def setup(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> tuple[Supervisor, Path]:
    """A supervisor with fake programs and a saved complete configuration."""
    calls = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(calls))
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict(CONFIG))
    logs = LogBuffer(echo=False)
    fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: logs.add("fan-control", text))
    supervisor = Supervisor(
        store,
        logs,
        tmp_path / "config" / "templog" / "temps.jsonl",
        fans,
        hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL),
        hdidle=make_script("hd-idle", "print('hd-idle', sys.argv[1:], flush=True); time.sleep(60)"),
    )
    return supervisor, calls


def test_log_buffer(capsys: pytest.CaptureFixture[str]) -> None:
    """Lines are numbered, trimmed and echoed with their source."""
    logs = LogBuffer(maxlen=2)
    for i in range(3):
        logs.add("x", f"line {i}\n")
    assert [line["text"] for line in logs.since(0)] == ["line 1", "line 2"]
    assert logs.since(3) == [] and logs.last_seq == 3
    logs.close()  # the echo thread writes what is queued
    assert "[x] line 2" in capsys.readouterr().out


def test_log_echo_never_raises_nor_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout gone (BrokenPipeError) or full (a FIFO nobody reads): add() returns at once, and keeps every line itself."""
    import threading

    release = threading.Event()

    class Stuck:
        """A stdout whose write waits until released, then fails."""

        def write(self, text: str) -> int:
            release.wait(5)
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(supervisor_module.sys, "stdout", Stuck())
    logs = LogBuffer(maxlen=5000)
    began = time.monotonic()
    for i in range(3000):
        logs.add("x", f"line {i}")
    assert time.monotonic() - began < 2
    assert logs.last_seq == 3000 and logs.since(2999)[0]["text"] == "line 2999"
    release.set()
    logs.close(2)


async def test_curve_mode(setup: tuple[Supervisor, Path]) -> None:
    """hddfancontrol only measures, the fan controller runs the curve, hd-idle runs, and all stop."""
    supervisor, calls = setup
    await supervisor.start()
    await wait_for(lambda: supervisor.hddfancontrol.state == "running" and supervisor.hdidle.state == "running")
    assert supervisor.mode == "running"
    assert supervisor.fans.mode == "curve" and supervisor.fans.active
    await wait_for(lambda: "--fan-cmd /bin/true" in read(calls))
    assert "--pwm" not in read(calls)
    assert "--temp-log" in read(calls)
    status = supervisor.status()
    assert status["daemons"]["hddfancontrol"]["pid"] and status["control_mode"] == "curve"
    await supervisor.stop()
    assert supervisor.mode == "stopped"
    assert supervisor.hddfancontrol.pid is None


async def test_auto_mode_and_apply(setup: tuple[Supervisor, Path]) -> None:
    """Auto mode runs hddfancontrol as a probe and the controller drives the fans."""
    supervisor, calls = setup
    await supervisor.start()
    await supervisor.apply(from_dict({**CONFIG, "control_mode": "auto"}))
    await wait_for(lambda: "--fan-cmd /bin/true" in read(calls))
    assert supervisor.fans.mode == "auto"
    assert supervisor.source == "file"
    await supervisor.stop()
    assert supervisor.fans.mode is None


async def test_manual_mode(setup: tuple[Supervisor, Path], fake_sys: Path) -> None:
    """Manual control writes the PWM files and goes back to automatic control."""
    supervisor, calls = setup
    await supervisor.start()
    with pytest.raises(Busy):
        supervisor.set_pwm(PWM1, 10)
    await supervisor.set_manual(True)
    await supervisor.set_manual(True)
    assert supervisor.mode == "manual"
    supervisor.set_pwm(PWM1, 99)
    assert (fake_sys / "class/hwmon/hwmon2/pwm1").read_text().strip() == "99"
    supervisor.set_pwm("/sys/class/hwmon/hwmon2/pwm2", 10)  # not a chosen fan: still allowed by hand
    assert (fake_sys / "class/hwmon/hwmon2/pwm2").read_text().strip() == "10"
    await wait_for(lambda: "--fan-cmd" in read(calls))
    await supervisor.set_manual(False)
    assert (fake_sys / "class/hwmon/hwmon2/pwm1_enable").read_text().strip() == "5"
    assert (fake_sys / "class/hwmon/hwmon2/pwm2").read_text().strip() == "255"  # its value is given back
    assert supervisor.mode == "running"
    await supervisor.stop()


async def test_manual_without_drives(tmp_path: Path, fake_sys: Path) -> None:
    """Manual control works without any setting, and warns that nothing is watched."""
    logs = LogBuffer(echo=False)
    supervisor = Supervisor(
        SettingsStore(tmp_path, {}), logs, None, FanController(PwmWriter(fake_sys), lambda: None, lambda _: None)
    )
    assert supervisor.mode == "waiting"
    await supervisor.start()
    await supervisor.set_manual(True)
    assert any("cannot watch" in line["text"] for line in logs.since(0))
    await supervisor.stop()


async def test_pwm_test(setup: tuple[Supervisor, Path]) -> None:
    """The test stops the daemons, finds the values, and starts everything again."""
    supervisor, _ = setup
    await supervisor.start()
    await supervisor.start_pwm_test([PWM1, "/sys/class/hwmon/hwmon2/pwm2"])
    assert supervisor.mode == "pwm_test"
    with pytest.raises(Busy):
        await supervisor.restart()
    with pytest.raises(Busy):
        await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running)
    results = supervisor.status()["pwm_test"]["results"]
    assert results[PWM1] == {"status": "done", "start": 180, "stop": 62}
    assert results["/sys/class/hwmon/hwmon2/pwm2"] == {"status": "failed", "error": "no RPM change"}
    await wait_for(lambda: supervisor.hddfancontrol.state == "running")
    await supervisor.stop()


async def test_pwm_test_takes_the_output_and_gives_it_back(
    setup: tuple[Supervisor, Path], fake_sys: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An output driven by the chip is put in manual mode for the test, then given back."""
    supervisor, calls = setup
    monkeypatch.setenv("FAKE_SYS", str(fake_sys))
    enable = fake_sys / "class/hwmon/hwmon2/pwm1_enable"
    assert enable.read_text().strip() == "5"
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running)
    assert "enable during test: 1" in read(calls)
    # Fan control takes the output again after the test; stopping gives it back to the chip.
    await supervisor.stop()
    assert enable.read_text().strip() == "5"


async def test_pwm_test_cancel(setup: tuple[Supervisor, Path], make_script: ScriptMaker) -> None:
    """Cancelling marks the running test and restarts the daemons."""
    supervisor, _ = setup
    supervisor.hddfancontrol.binary = make_script("slow", "print('testing', flush=True); time.sleep(60)")
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: PWM1 in supervisor.test.results)
    await supervisor.cancel_pwm_test()
    assert supervisor.test.results[PWM1]["status"] == "cancelled"
    assert not supervisor.test.running
    await supervisor.stop()


async def test_pwm_test_failures(setup: tuple[Supervisor, Path], make_script: ScriptMaker) -> None:
    """A missing program or a silent exit is a failed test."""
    supervisor, _ = setup
    supervisor.hddfancontrol.binary = "/nonexistent/hddfancontrol"
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running)
    assert supervisor.test.results[PWM1]["status"] == "failed"
    await supervisor.stop()
    supervisor.hddfancontrol.binary = make_script("quiet", "sys.exit(3)")
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running)
    assert supervisor.test.results[PWM1] == {"status": "failed", "error": "exit code 3"}
    await supervisor.stop()


async def test_reset_and_incomplete(setup: tuple[Supervisor, Path]) -> None:
    """Reset goes back to the (empty) environment, which waits for settings."""
    supervisor, _ = setup
    await supervisor.start()
    await supervisor.reset()
    assert supervisor.source == "environment" and supervisor.mode == "waiting"
    assert any("not controlled" in line["text"] for line in supervisor.logs.since(0))
    await supervisor.stop()


async def test_daemon_restarts(tmp_path: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that exits is started again, with a growing delay; a missing one keeps trying."""
    real = asyncio.sleep
    delays: list[float] = []

    async def quick(delay: float) -> None:
        delays.append(delay)
        await real(0.01)

    monkeypatch.setattr(supervisor_module.asyncio, "sleep", quick)
    logs = LogBuffer(echo=False)
    daemon = Daemon("x", make_script("exits", "print('bye'); sys.exit(1)"), logs)
    daemon.start([])
    await wait_for(lambda: daemon.restarts >= 3)
    await daemon.stop()
    # wait_for sleeps too: keep only the restart delays.
    assert [d for d in delays if d >= 1][:3] == [5.0, 10.0, 20.0]
    assert daemon.last_exit == 1 and daemon.state == "stopped"

    missing = Daemon("y", str(tmp_path / "missing"), logs)
    missing.start([])
    await wait_for(lambda: missing.restarts >= 1)
    assert any("Cannot start" in line["text"] for line in logs.since(0))
    await missing.stop()


async def test_long_lines_and_kill(make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlong lines are skipped; a program that ignores SIGTERM is killed."""
    monkeypatch.setattr(supervisor_module, "STOP_TIMEOUT", 0.2)
    logs = LogBuffer(echo=False)
    body = (
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('x' * 200000, flush=True)\n"
        "print('after', flush=True)\n"
        "time.sleep(60)"
    )
    daemon = Daemon("z", make_script("stubborn", body), logs)
    daemon.start([])
    await wait_for(lambda: any(line["text"] == "after" for line in logs.since(0)))
    await daemon.stop()
    assert any("too long" in line["text"] for line in logs.since(0))


def test_settings_error_in_status(tmp_path: Path, fake_sys: Path) -> None:
    """A broken settings file shows in the status."""
    (tmp_path / "settings.json").write_text(json.dumps({"interval_s": -1}))
    supervisor = Supervisor(
        SettingsStore(tmp_path, {}),
        LogBuffer(echo=False),
        None,
        FanController(PwmWriter(fake_sys), lambda: None, lambda _: None),
    )
    assert supervisor.status()["settings_error"][0]["field"] == "interval_s"


async def test_read_only(setup: tuple[Supervisor, Path], fake_sys: Path) -> None:
    """With /sys read only nothing starts, and manual control and the PWM test are refused."""
    supervisor, calls = setup
    supervisor.read_only = True
    await supervisor.start()
    assert supervisor.mode == "read_only" and supervisor.status()["read_only"] is True
    assert not supervisor.hddfancontrol.active and not supervisor.hdidle.active and not supervisor.fans.active
    assert any("read only" in line["text"] for line in supervisor.logs.since(0))
    with pytest.raises(ReadOnly):
        await supervisor.set_manual(True)
    with pytest.raises(ReadOnly):
        await supervisor.start_pwm_test([PWM1])
    assert not supervisor.manual and not supervisor.test.running
    assert (fake_sys / "class/hwmon/hwmon2/pwm1_enable").read_text().strip() == "5"
    await supervisor.stop()


async def test_fans_follow_their_chip(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan is found again when the hwmon number of its chip changes; a missing chip stops its control."""
    calls = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(calls))
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict(CONFIG))
    chip = "/sys/devices/platform/nct6775.656"
    moved: list[tuple[str, str]] = []
    hddfancontrol = make_script("hddfancontrol", FAKE_HDDFANCONTROL)
    hdidle = make_script("hd-idle", "time.sleep(60)")

    def make() -> Supervisor:
        logs = LogBuffer(echo=False)
        fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: None)
        return Supervisor(
            store,
            logs,
            None,
            fans,
            hddfancontrol=hddfancontrol,
            hdidle=hdidle,
            hardware=Hardware(fake_dev, fake_sys),
            on_pwm_moved=lambda old, new: moved.append((old, new)),
        )

    supervisor = make()
    await supervisor.start()
    assert supervisor.settings.pwms[0].chip == chip
    assert store.load()[0].pwms[0].chip == chip  # recorded for the next start
    await supervisor.stop()

    (fake_sys / "class/hwmon/hwmon2").rename(fake_sys / "class/hwmon/hwmon7")
    moved_to = "/sys/class/hwmon/hwmon7/pwm1"
    supervisor = make()
    await supervisor.start()
    assert supervisor.settings.pwms[0].path == moved_to and store.load()[0].pwms[0].path == moved_to
    assert moved == [(PWM1, moved_to)]
    await wait_for(lambda: moved_to in supervisor.fans.writer.values)
    await supervisor.stop()

    (fake_sys / "class/hwmon/hwmon7").rename(tmp_path / "gone")
    supervisor = make()
    await supervisor.start()
    assert supervisor.missing_pwms == [moved_to] and supervisor.status()["missing_pwms"] == [moved_to]
    # The drives are still watched: only the missing fan is left alone.
    assert supervisor.hddfancontrol.active and not supervisor.fans.active
    assert any("not on the host" in line["text"] for line in supervisor.logs.since(0))
    # Nothing changed yet: a look finds nothing to do.
    assert not await supervisor.recheck_fans()
    # The chip comes back (its module loaded late): the fan is controlled again.
    (tmp_path / "gone").rename(fake_sys / "class/hwmon/hwmon3")
    assert await supervisor.recheck_fans()
    back = "/sys/class/hwmon/hwmon3/pwm1"
    assert supervisor.missing_pwms == [] and supervisor.settings.pwms[0].path == back
    assert store.load()[0].pwms[0].path == back
    await wait_for(lambda: back in supervisor.fans.writer.values)
    assert any("came back" in line["text"] for line in supervisor.logs.since(0))
    await supervisor.stop()


async def test_missing_chip_is_looked_for_again(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a fan's chip is missing, the supervisor looks for it every place_period and starts its control when it is back."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict(CONFIG))
    logs = LogBuffer(echo=False)
    fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: None)
    supervisor = Supervisor(
        store,
        logs,
        None,
        fans,
        hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        hardware=Hardware(fake_dev, fake_sys),
        place_period=0.05,
    )
    await supervisor.start()  # records the chip
    await supervisor.stop()
    (fake_sys / "class/hwmon/hwmon2").rename(tmp_path / "gone")
    await supervisor.start()
    assert supervisor.missing_pwms == [PWM1] and not fans.active
    await asyncio.sleep(0.2)
    assert supervisor.missing_pwms == [PWM1]
    (tmp_path / "gone").rename(fake_sys / "class/hwmon/hwmon2")
    await wait_for(lambda: not supervisor.missing_pwms and fans.active)
    await supervisor.stop()
    assert supervisor._place_watch is None or supervisor._place_watch.done()


async def test_a_chip_from_the_api_is_checked(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply ignores a chip sent with a fan that is not the chip of its PWM file: it can never hide a fan from control."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    store = SettingsStore(tmp_path / "config", {})
    fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: None)
    supervisor = Supervisor(
        store,
        LogBuffer(echo=False),
        None,
        fans,
        hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        hardware=Hardware(fake_dev, fake_sys),
    )
    bogus = {**CONFIG, "pwms": [{"path": PWM1, "start": 200, "stop": 75, "chip": "/sys/devices/platform/it87.552"}]}
    await supervisor.apply(from_dict(bogus))
    assert supervisor.missing_pwms == [] and supervisor.settings.pwms[0].chip == "/sys/devices/platform/nct6775.656"
    await wait_for(lambda: fans.active and PWM1 in fans.writer.values)
    await supervisor.stop()


async def test_manual_ends_without_a_page(setup: tuple[Supervisor, Path], fake_sys: Path) -> None:
    """Manual control ends by itself when no page asks for the status any more."""
    supervisor, _ = setup
    supervisor._manual_timeout = 0.2
    supervisor._manual_watch_period = 0.05
    await supervisor.start()
    await supervisor.set_manual(True)
    supervisor.set_pwm(PWM1, 99)
    for _ in range(6):  # an open page keeps it going
        await asyncio.sleep(0.05)
        supervisor.touch_manual()
    assert supervisor.manual
    # Then manual control ends and fan control takes over again.
    await wait_for(lambda: not supervisor.manual and supervisor.fans.mode == "curve")
    assert any("leaving manual control" in line["text"] for line in supervisor.logs.since(0))
    await supervisor.stop()


async def test_manual_watch_waits_for_the_pwm_test(setup: tuple[Supervisor, Path]) -> None:
    """During a PWM test the watch cannot leave manual control; it does once the test is over."""
    supervisor, _ = setup
    supervisor._manual_timeout = 0.1
    supervisor._manual_watch_period = 0.05
    await supervisor.start()
    await supervisor.set_manual(True)
    supervisor.test.running = True
    await asyncio.sleep(0.3)
    assert supervisor.manual
    supervisor.test.running = False
    await wait_for(lambda: not supervisor.manual)
    await supervisor.stop()


async def test_full_speed_while_hddfancontrol_is_down(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hddfancontrol ending by itself puts the fans at full speed at once, until it measures again."""
    real = asyncio.sleep

    async def quick(delay: float) -> None:
        await real(0.01)

    monkeypatch.setattr(supervisor_module.asyncio, "sleep", quick)
    from hddfancontrol_webui import fancontrol as fancontrol_module

    monkeypatch.setattr(fancontrol_module.asyncio, "sleep", quick)
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict({**CONFIG, "drive_temp_min": 30, "drive_temp_max": 50}))
    logs = LogBuffer(echo=False)
    sample = {"value": {"time": "2999-01-01T00:00:00+00:00", "temps": {"sda X": 20.0}}}
    fans = FanController(PwmWriter(fake_sys), lambda: sample["value"], lambda text: logs.add("fan-control", text))
    supervisor = Supervisor(store, logs, None, fans, hddfancontrol=make_script("crash", "sys.exit(3)"), hdidle="/bin/true")
    value = fake_sys / "class" / "hwmon" / "hwmon2" / "pwm1"
    await supervisor.start()
    await wait_for(lambda: fans.auto.failsafe and value.read_text().strip() == "255")
    assert any("until it measures again" in line["text"] for line in logs.since(0))
    # A newer sample: control resumes.
    supervisor.hddfancontrol._on_exit = None
    sample["value"] = {"time": "2999-01-01T00:01:00+00:00", "temps": {"sda X": 20.0}}
    await wait_for(lambda: not fans.auto.failsafe)
    await supervisor.stop()
    assert (fake_sys / "class" / "hwmon" / "hwmon2" / "pwm1_enable").read_text().strip() == "5"


async def test_a_daemon_run_as_another_user_gets_no_terminal(
    make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway (user=) reads nothing from the core's stdin and runs in a session of its own.

    Started by hand from a root shell, a gateway taken over could
    otherwise push keystrokes into that terminal (TIOCSTI). The change of
    user needs root: here it is recorded and left out, the rest runs for real.
    """
    import os

    real = asyncio.create_subprocess_exec
    changes: list[dict[str, Any]] = []

    async def as_me(*args: Any, **kwargs: Any) -> Any:
        changes.append({key: kwargs.pop(key) for key in ("user", "group", "extra_groups") if key in kwargs})
        return await real(*args, **kwargs)

    monkeypatch.setattr(supervisor_module.asyncio, "create_subprocess_exec", as_me)
    logs = LogBuffer(echo=False)
    body = (
        "import os\n"
        "print('stdin', os.path.realpath('/proc/self/fd/0'), flush=True)\n"
        "print('session', os.getsid(0) == os.getpid(), flush=True)\n"
        "time.sleep(60)\n"
    )
    daemon = Daemon("gateway", make_script("gateway", body), logs, user=(os.getuid(), os.getgid()))
    daemon.start([])
    await wait_for(lambda: any(line["text"].startswith("session") for line in logs.since(0)))
    await daemon.stop()
    texts = [line["text"] for line in logs.since(0)]
    assert "stdin /dev/null" in texts and "session True" in texts
    assert changes[0] == {"user": os.getuid(), "group": os.getgid(), "extra_groups": []}


async def test_daemon_hook_errors_are_logged(make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing hook is logged and the daemon keeps restarting."""
    real = asyncio.sleep

    async def quick(_: float) -> None:
        await real(0.01)

    monkeypatch.setattr(supervisor_module.asyncio, "sleep", quick)
    logs = LogBuffer(echo=False)

    def broken() -> None:
        raise RuntimeError("hook")

    daemon = Daemon("x", make_script("exits2", "sys.exit(1)"), logs, on_exit=broken)
    daemon.start([])
    await wait_for(lambda: daemon.restarts >= 2)
    await daemon.stop()
    assert any("RuntimeError('hook')" in line["text"] for line in logs.since(0))


async def test_pwm_test_starts_again_under_the_lock(setup: tuple[Supervisor, Path]) -> None:
    """The end of the test waits for the lock: until then it still runs, and refuses other changes."""
    supervisor, _ = setup
    await supervisor.start_pwm_test([PWM1])
    async with supervisor._lock:
        await asyncio.sleep(0.5)
        # hddfancontrol only measures meanwhile (for the watch); the fan control waits.
        assert supervisor.test.running and not supervisor.fans.active
    await wait_for(lambda: not supervisor.test.running and supervisor.fans.active)
    await supervisor.stop()


async def test_set_pwm_while_manual_control_restarts(setup: tuple[Supervisor, Path]) -> None:
    """A value sent while manual control is on but its controller is not running yet is Busy, not a crash."""
    supervisor, _ = setup
    supervisor.manual = True
    with pytest.raises(Busy):
        supervisor.set_pwm(PWM1, 10)
    supervisor.manual = False


async def test_fans_must_be_pwm_outputs_of_the_host(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply refuses a fan that is no PWM output of the host; at start such a fan is not controlled, manual mode too."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    store = SettingsStore(tmp_path / "config", {})
    logs = LogBuffer(echo=False)
    fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: None)
    supervisor = Supervisor(
        store,
        logs,
        None,
        fans,
        hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        hardware=Hardware(fake_dev, fake_sys),
    )
    ghost = "/sys/class/hwmon/hwmon9/pwm1"  # no such chip on this host, and no chip recorded
    doc = {**CONFIG, "pwms": [{"path": PWM1, "start": 200, "stop": 75}, {"path": ghost, "start": 200, "stop": 75}]}
    with pytest.raises(SettingsError) as refused:
        await supervisor.apply(from_dict(doc))
    assert refused.value.problems == [{"field": "pwms.1.path", "code": "unknown_pwm", "params": {}}]
    assert not store.path.exists()
    # The same document already saved (an older version, or written by hand): kept, but that fan is not controlled.
    store.save(from_dict(doc))
    supervisor.settings, supervisor.source = store.load()
    await supervisor.start()
    # The other fan is still controlled, and the drives watched.
    assert supervisor.missing_pwms == [ghost] and supervisor.hddfancontrol.active
    await wait_for(lambda: PWM1 in supervisor.fans.writer.values)
    assert ghost not in supervisor.fans.writer.values
    # Manual control never writes it either, not even at full speed for safety.
    await supervisor.set_manual(True)
    supervisor.fans._full_speed([p.path for p in supervisor.fans._settings.pwms])
    assert (
        PWM1 in supervisor.fans.writer.values
        and ghost not in supervisor.fans.writer.values
        and not (fake_sys / "class/hwmon/hwmon9").exists()
    )
    await supervisor.stop()


async def test_release_fans_gives_them_back_before_anything_else(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At SIGTERM the fans go back to the chip first, even while a daemon is slow to stop; nothing starts them again."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict(CONFIG))
    logs = LogBuffer(echo=False)
    fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: None)
    # hd-idle ignores SIGTERM: its stop takes the whole STOP_TIMEOUT.
    stubborn = make_script("hd-idle", "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
    supervisor = Supervisor(
        store, logs, None, fans, hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL), hdidle=stubborn
    )
    supervisor.settings = from_dict({**CONFIG, "spin_down_time_s": 600})
    await supervisor.start()
    await supervisor.set_manual(True)
    supervisor.set_pwm(PWM1, 40)
    assert read(fake_sys / "class/hwmon/hwmon2/pwm1_enable") == "1\n"
    monkeypatch.setattr(supervisor_module, "STOP_TIMEOUT", 30.0)
    await asyncio.wait_for(supervisor.release_fans(), 2)  # never waits for the daemons
    assert read(fake_sys / "class/hwmon/hwmon2/pwm1_enable") == "5\n"  # the chip drives it again
    with pytest.raises(Busy):
        await supervisor.set_manual(False)
    with pytest.raises(Busy):
        await supervisor.apply(from_dict(CONFIG))
    monkeypatch.setattr(supervisor_module, "STOP_TIMEOUT", 0.2)
    await supervisor.stop()
    assert read(fake_sys / "class/hwmon/hwmon2/pwm1_enable") == "5\n"


async def test_release_fans_during_a_pwm_test_starts_nothing(
    setup: tuple[Supervisor, Path], fake_sys: Path, make_script: ScriptMaker
) -> None:
    """A PWM test running at SIGTERM is cancelled, its outputs given back, and the control is not started again."""
    supervisor, _calls = setup
    supervisor.hddfancontrol.binary = make_script("hddfancontrol", "time.sleep(60)")
    await supervisor.start()
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: supervisor.test.current == PWM1 and PWM1 in supervisor.fans.writer._saved)
    await supervisor.release_fans()
    assert not supervisor.test.running and supervisor.mode != "running"
    assert not supervisor.hddfancontrol.active and not supervisor.fans.active
    assert read(fake_sys / "class/hwmon/hwmon2/pwm1_enable") == "5\n"
    await supervisor.stop()


async def test_apply_refuses_a_drive_path_that_is_no_block_device(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive name that leads to something other than a block device is refused; one that leads nowhere is kept."""
    from hddfancontrol_webui import hardware as hardware_module

    monkeypatch.setattr(hardware_module, "_IS_DRIVE_NODE", __import__("stat").S_ISBLK)  # the real check
    store = SettingsStore(tmp_path / "config", {})
    fans = FanController(PwmWriter(fake_sys), lambda: None, lambda text: None)
    supervisor = Supervisor(
        store,
        LogBuffer(echo=False),
        None,
        fans,
        hddfancontrol=make_script("hddfancontrol", "time.sleep(60)"),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        hardware=Hardware(fake_dev, fake_sys),
    )
    (fake_dev / "disk/by-id/ata-EVIL").symlink_to("/dev/null")  # a character device
    doc = {"drives": ["/dev/disk/by-id/ata-EVIL", "/dev/sdz"], "monitor_drives": ["/dev/sda"], "pwms": []}
    with pytest.raises(SettingsError) as refused:
        await supervisor.apply(from_dict(doc))
    assert [p["field"] for p in refused.value.problems] == ["drives.0", "monitor_drives.0"]  # /dev/sda: a plain file here
    assert {p["code"] for p in refused.value.problems} == {"not_block_device"}
    await supervisor.stop()


SLOW_PWM_TEST = """
if "pwm-test" in sys.argv:
    print("INFO Testing fan, this may take a long time", flush=True)
    time.sleep(60)
print("running", flush=True)
time.sleep(60)
"""


def watched_supervisor(tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, latest: Any) -> Supervisor:
    """A supervisor whose fan controller reads the temperatures from latest()."""
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict({**CONFIG, "drives": ["/dev/sda"], "drive_temp_max": 50, "target_temp": 40}))
    logs = LogBuffer(echo=False)
    fans = FanController(PwmWriter(fake_sys), latest, lambda text: None, lambda: {"/dev/sda": "sda"})
    return Supervisor(
        store,
        logs,
        None,
        fans,
        hddfancontrol=make_script("hddfancontrol", SLOW_PWM_TEST),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
    )


async def test_pwm_test_stops_when_a_drive_gets_hot(tmp_path: Path, fake_sys: Path, make_script: ScriptMaker) -> None:
    """A drive at its maximum during the test cancels it; the outputs go back and the control starts again."""
    sample: dict[str, Any] = {"time": datetime.now(UTC).isoformat(), "temps": {"sda": 40.0}}
    supervisor = watched_supervisor(tmp_path, fake_sys, make_script, lambda: sample)
    await supervisor.start()
    await supervisor.start_pwm_test([PWM1])
    assert supervisor.hddfancontrol.active  # still measuring, for the watch
    await asyncio.sleep(1.2)
    assert supervisor.test.running
    sample = {"time": datetime.now(UTC).isoformat(), "temps": {"sda": 50.0}}
    await wait_for(lambda: not supervisor.test.running, 4)
    assert supervisor.test.stopped_by == "hot" and supervisor.test.results[PWM1]["status"] == "cancelled"
    assert any("PWM test stops" in line["text"] for line in supervisor.logs.since(0))
    await wait_for(lambda: supervisor.fans.active)
    await supervisor.stop()


async def test_pwm_test_stops_when_the_temperatures_stop(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sample newer than the stale limit during the test cancels it too."""
    old = {"time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(), "temps": {"sda": 40.0}}
    supervisor = watched_supervisor(tmp_path, fake_sys, make_script, lambda: old)
    monkeypatch.setattr(supervisor_module, "PWM_TEST_STALE_MIN_S", 0.0)  # stale after 3 intervals (3 s)
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running, 6)
    assert supervisor.test.stopped_by == "stale"
    await supervisor.stop()


async def test_each_output_has_a_time_limit(
    setup: tuple[Supervisor, Path], make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test of one output that never ends is stopped at PWM_TEST_MAX_S and counted as failed."""
    supervisor, _ = setup
    supervisor.hddfancontrol.binary = make_script("hddfancontrol", SLOW_PWM_TEST)
    monkeypatch.setattr(supervisor_module, "PWM_TEST_MAX_S", 0.5)
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running, 4)
    assert supervisor.test.results[PWM1] == {"status": "failed", "error": "timeout"}
    await supervisor.stop()


async def test_pwm_test_ends_when_its_watch_fails(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error of the watch stops the test, and the outputs are still given back.

    The test must never stay running: every change would answer Busy and the
    output would stay in manual mode at the last value of pwm-test.
    """
    sample: dict[str, Any] = {"time": datetime.now(UTC).isoformat(), "temps": {"sda": 40.0}}
    supervisor = watched_supervisor(tmp_path, fake_sys, make_script, lambda: sample)
    monkeypatch.setattr(supervisor_module, "PWM_TEST_MAX_S", 3.0)

    async def broken(*args: Any) -> Any:
        raise RuntimeError("watch broken")

    restored: list[bool] = []
    restore = supervisor.fans.writer.restore

    def counted() -> None:
        restored.append(True)
        restore()

    monkeypatch.setattr(supervisor.fans, "watch_drives", broken)
    monkeypatch.setattr(supervisor.fans.writer, "restore", counted)
    await supervisor.start_pwm_test([PWM1])
    await wait_for(lambda: not supervisor.test.running, 6)
    assert supervisor.test.stopped_by == "error"
    assert supervisor.test.results[PWM1]["status"] == "cancelled"
    assert restored
    assert any("watch broken" in line["text"] for line in supervisor.logs.since(0))
    await supervisor.restart()  # not Busy
    await supervisor.stop()


async def test_a_full_config_never_stops_the_start(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hwmon number changed and /config cannot be written: the fans are controlled anyway.

    Saving the corrected path and moving the label are extras of the start
    (the next start finds the chip again); an apply still saves the
    settings, but a label that cannot follow never makes it fail.
    """
    # The fan was recorded with its chip at an earlier start, under hwmon2; after a reboot the chip is hwmon7.
    store = SettingsStore(tmp_path / "config", {})
    settings = from_dict(CONFIG)
    chip = Hardware(fake_dev, fake_sys).chip_of(PWM1)
    store.save(replace(settings, pwms=(replace(settings.pwms[0], chip=chip),)))
    (fake_sys / "class/hwmon/hwmon2").rename(fake_sys / "class/hwmon/hwmon7")
    moved_to = "/sys/class/hwmon/hwmon7/pwm1"

    def full(*args: Any) -> None:
        raise OSError(28, "No space left on device")

    logs = LogBuffer(echo=False)
    supervisor = Supervisor(
        store,
        logs,
        None,
        FanController(PwmWriter(fake_sys), lambda: None, lambda text: None),
        hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        hardware=Hardware(fake_dev, fake_sys),
        on_pwm_moved=full,
    )
    monkeypatch.setattr(store, "save", full)
    await supervisor.start()
    assert supervisor.settings.pwms[0].path == moved_to
    assert supervisor.hddfancontrol.active and supervisor.fans.active
    texts = [line["text"] for line in logs.since(0)]
    assert any(text.startswith("ERROR") and "label" in text for text in texts)
    assert any(text.startswith("ERROR") and "settings" in text for text in texts)
    monkeypatch.undo()
    await supervisor.apply(supervisor.settings)  # the label still cannot follow: the apply goes on
    assert supervisor.fans.active
    await supervisor.stop()


async def test_a_restart_never_reads_the_sample_of_the_run_before(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker
) -> None:
    """After an apply that drops an extra sensor, the old sample is not read with the new layout.

    hddfancontrol logs the drives first and the sensors last. Read with one
    sensor fewer, the last sample of the run before makes the processor
    (72 °C) a drive; the new run has written nothing yet, so there is no
    sample at all until it does.
    """
    log = tmp_path / "templog" / "temps.jsonl"
    log.parent.mkdir()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    measures = [{"device": "sda X", "temp_celcius": 40.0}, {"device": "cpu", "temp_celcius": 72.0}]
    log.write_text(json.dumps({"time_utc": now, "measures": measures}) + "\n")
    temp_log = TempLog(log)
    store = SettingsStore(tmp_path / "config", {})
    sensor = {"path": "/sys/class/hwmon/hwmon10/temp1_input", "temp_min": 60, "temp_max": 90}
    store.save(from_dict({**CONFIG, "drives": ["/dev/sda"], "hwmons": [sensor], "drive_temp_max": 50}))
    fans = FanController(PwmWriter(fake_sys), temp_log.latest, lambda text: None, lambda: {"/dev/sda": "sda"})
    supervisor = Supervisor(
        store,
        LogBuffer(echo=False),
        log,
        fans,
        hddfancontrol=make_script("hddfancontrol", "time.sleep(60)"),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        fence_samples=temp_log.fence,
    )
    await supervisor.start()
    await supervisor.apply(from_dict({**CONFIG, "drives": ["/dev/sda"], "drive_temp_max": 50}))
    await wait_for(lambda: fans.auto.speed is not None)
    assert fans.auto.hottest is None and temp_log.latest() is None
    # The first sample of the new run counts again.
    later = (datetime.now(UTC) + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log.open("a") as handle:
        handle.write(json.dumps({"time_utc": later, "measures": measures[:1]}) + "\n")
    assert temp_log.latest() == {"time": datetime.fromisoformat(later).isoformat(), "temps": {"sda X": 40.0}}
    await supervisor.stop()


async def test_full_speed_in_manual_control_when_hddfancontrol_ends(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In manual control too, hddfancontrol ending puts every fan at full speed at once (safety_reason stale).

    Without it, a hot drive would go unseen until the stale limit,
    max(3 x interval, 120 s), even though nothing measures any more.
    """
    real = asyncio.sleep

    async def quick(delay: float) -> None:
        await real(0.01)

    monkeypatch.setattr(supervisor_module.asyncio, "sleep", quick)
    from hddfancontrol_webui import fancontrol as fancontrol_module

    monkeypatch.setattr(fancontrol_module.asyncio, "sleep", quick)
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict({**CONFIG, "drives": ["/dev/sda"], "drive_temp_max": 50}))
    logs = LogBuffer(echo=False)
    # A sample from the future: never stale by age, so only the end of hddfancontrol can count.
    sample = {"time": "2999-01-01T00:00:00+00:00", "temps": {"sda X": 20.0}}
    fans = FanController(
        PwmWriter(fake_sys), lambda: sample, lambda text: logs.add("fan-control", text), lambda: {"/dev/sda": "sda"}
    )
    crash = make_script("crash", "time.sleep(0.3); sys.exit(3)")
    supervisor = Supervisor(store, logs, None, fans, hddfancontrol=crash, hdidle="/bin/true")
    await supervisor.set_manual(True)
    supervisor.set_pwm(PWM1, 60)
    await wait_for(lambda: fans.safety, 3)
    assert fans.safety_reason == "stale"
    assert (fake_sys / "class" / "hwmon" / "hwmon2" / "pwm1").read_text().strip() == "255"
    supervisor.hddfancontrol._on_exit = None
    await supervisor.stop()


def test_log_buffer_from_many_threads() -> None:
    """Lines added from worker threads (the hooks of Daemon) and the event loop at once each get their own number."""
    import sys
    import threading

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        logs = LogBuffer(maxlen=200_000, echo=False)
        threads = [threading.Thread(target=lambda: [logs.add("x", "line") for _ in range(20_000)]) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(old)
    seqs = [line["seq"] for line in logs.since(0)]
    assert logs.last_seq == 160_000 and len(set(seqs)) == 160_000


async def test_outputs_a_killed_run_left_are_given_back_at_start(
    tmp_path: Path, fake_sys: Path, make_script: ScriptMaker
) -> None:
    """An output that a killed run took, and that this run does not drive, gets the chip's mode back at start.

    Otherwise it would stay in manual mode at the killed run's last value
    until a reboot: a fan taken out of the settings, or one moved by a
    slider in manual control.
    """
    pwm2 = "/sys/class/hwmon/hwmon2/pwm2"
    enable2 = fake_sys / "class/hwmon/hwmon2/pwm2_enable"
    enable2.write_text("2\n")  # the chip drives it
    state = tmp_path / "config" / "pwm-state.json"
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n")
    PwmWriter(fake_sys, state, boot_id).write(pwm2, 40)  # then SIGKILL: no restore
    assert enable2.read_text().strip() == "1"
    store = SettingsStore(tmp_path / "config", {})
    store.save(from_dict(CONFIG))  # drives pwm1 only
    logs = LogBuffer(echo=False)
    fans = FanController(PwmWriter(fake_sys, state, boot_id), lambda: None, lambda text: None)
    supervisor = Supervisor(
        store,
        logs,
        None,
        fans,
        hddfancontrol=make_script("hddfancontrol", "time.sleep(60)"),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
    )
    await supervisor.start()
    assert enable2.read_text().strip() == "2"
    assert pwm2 not in json.loads(state.read_text())["outputs"]
    assert any(pwm2 in line["text"] for line in logs.since(0))
    await supervisor.stop()


async def test_monitored_drives_change_without_a_restart(setup: tuple[Supervisor, Path]) -> None:
    """set_monitor_drives saves the list and restarts nothing: only the monitor reads it."""
    supervisor, calls = setup
    await supervisor.start()
    await wait_for(lambda: "--fan-cmd /bin/true" in read(calls))
    runs = read(calls).count("--fan-cmd")
    await supervisor.set_monitor_drives(("/dev/sdb",))
    assert supervisor.settings.monitor_drives == ("/dev/sdb",)
    assert supervisor.store.load()[0].monitor_drives == ("/dev/sdb",)
    await asyncio.sleep(0.2)
    assert read(calls).count("--fan-cmd") == runs
    await supervisor.release_fans()
    with pytest.raises(Busy):
        await supervisor.set_monitor_drives(())
    await supervisor.stop()


async def test_recheck_says_which_fans_came_back_and_which_are_gone(
    tmp_path: Path, fake_sys: Path, fake_dev: Path, make_script: ScriptMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A look that finds one more chip gone says so; a look that finds them back names only those, under their new path."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    # A second chip, with its own fan.
    other = fake_sys / "class/hwmon/hwmon5"
    other.mkdir()
    for name, value in {"name": "it8613", "pwm1": "100", "pwm1_enable": "2", "fan1_input": "700"}.items():
        (other / name).write_text(value + "\n")
    (fake_sys / "devices/platform/it87.2608").mkdir(parents=True)
    (other / "device").symlink_to("../../../devices/platform/it87.2608")
    pwm_b = "/sys/class/hwmon/hwmon5/pwm1"
    store = SettingsStore(tmp_path / "config", {})
    store.save(
        from_dict({**CONFIG, "pwms": [{"path": PWM1, "start": 200, "stop": 75}, {"path": pwm_b, "start": 200, "stop": 75}]})
    )
    logs = LogBuffer(echo=False)
    supervisor = Supervisor(
        store,
        logs,
        None,
        FanController(PwmWriter(fake_sys), lambda: None, lambda text: None),
        hddfancontrol=make_script("hddfancontrol", FAKE_HDDFANCONTROL),
        hdidle=make_script("hd-idle", "time.sleep(60)"),
        hardware=Hardware(fake_dev, fake_sys),
    )
    await supervisor.start()  # records both chips
    await supervisor.stop()
    other.rename(tmp_path / "gone_b")
    await supervisor.start()
    assert supervisor.missing_pwms == [pwm_b]

    def lines() -> list[str]:
        return [line["text"] for line in logs.since(0)]

    (fake_sys / "class/hwmon/hwmon2").rename(tmp_path / "gone_a")
    assert await supervisor.recheck_fans()
    assert supervisor.missing_pwms == [PWM1, pwm_b]
    assert not any("came back" in text for text in lines())
    assert any(text.endswith("gone too, they are not controlled: " + PWM1) for text in lines())

    (tmp_path / "gone_a").rename(fake_sys / "class/hwmon/hwmon3")
    assert await supervisor.recheck_fans()
    assert supervisor.missing_pwms == [pwm_b]
    came_back = [text for text in lines() if "came back" in text]
    assert came_back == ["The chip of these fans came back, they are controlled again: /sys/class/hwmon/hwmon3/pwm1"]
    await supervisor.stop()
