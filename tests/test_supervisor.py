"""Running, restarting and stopping the daemons, the modes and the PWM test."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptMaker

from hddfancontrol_webui import supervisor as supervisor_module
from hddfancontrol_webui.fancontrol import FanController, PwmWriter
from hddfancontrol_webui.hardware import Hardware
from hddfancontrol_webui.settings import SettingsStore, from_dict
from hddfancontrol_webui.supervisor import Busy, Daemon, LogBuffer, ReadOnly, Supervisor

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
    assert "[x] line 2" in capsys.readouterr().out


async def test_curve_mode(setup: tuple[Supervisor, Path]) -> None:
    """hddfancontrol drives the fans, hd-idle runs, and both stop."""
    supervisor, calls = setup
    await supervisor.start()
    await wait_for(lambda: supervisor.hddfancontrol.state == "running" and supervisor.hdidle.state == "running")
    assert supervisor.mode == "running"
    await wait_for(lambda: "--pwm" in read(calls))
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
    assert enable.read_text().strip() == "5"
    await supervisor.stop()


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
    await wait_for(lambda: moved_to in read(calls))
    await supervisor.stop()

    (fake_sys / "class/hwmon/hwmon7").rename(tmp_path / "gone")
    supervisor = make()
    await supervisor.start()
    assert supervisor.missing_pwms == [moved_to] and supervisor.status()["missing_pwms"] == [moved_to]
    assert not supervisor.hddfancontrol.active
    assert any("not on the host" in line["text"] for line in supervisor.logs.since(0))
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
    # The fans are given back by the restart that follows.
    await wait_for(lambda: (fake_sys / "class/hwmon/hwmon2/pwm1_enable").read_text().strip() == "5")
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
