"""Tests for the start of the core (__main__.startup): what may hold it up and what may not."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from hddfancontrol_webui.__main__ import startup
from hddfancontrol_webui.aio import cancel_and_wait
from hddfancontrol_webui.sensors import SensorReport


async def test_hung_checks_never_hold_up_the_fans() -> None:
    """Checks that never end (an open() on a dying drive) leave the scan, the fans and the monitor to start."""
    release = threading.Event()
    events: list[str] = []
    state: dict[str, Any] = {"checks": []}

    def checks() -> list[dict[str, Any]]:
        release.wait(5)
        return [{"code": "late"}]

    async def scan() -> SensorReport:
        events.append("scan")
        return SensorReport(state="done")

    async def start_fans() -> None:
        events.append("fans")

    await asyncio.wait_for(
        startup(state, checks, scan, start_fans, lambda: events.append("monitor"), lambda source, text: None), 2
    )
    assert events == ["scan", "fans", "monitor"]
    assert state["checks"] == [] and not state["checks_task"].done()
    release.set()
    await asyncio.wait_for(state["checks_task"], 5)
    assert state["checks"] == [{"code": "late"}]


async def test_failures_of_the_start() -> None:
    """A failing check or scan is logged and the start goes on; a failing fan start is logged and raised, the monitor started."""
    lines: list[tuple[str, str]] = []
    events: list[str] = []
    state: dict[str, Any] = {"checks": []}

    def checks() -> list[dict[str, Any]]:
        raise OSError("no /dev")

    async def scan() -> SensorReport:
        raise RuntimeError("no sensors-detect")

    async def start_fans() -> None:
        raise RuntimeError("no fans")

    with pytest.raises(RuntimeError, match="no fans"):
        await startup(state, checks, scan, start_fans, lambda: events.append("monitor"), lambda s, t: lines.append((s, t)))
    await state["checks_task"]
    assert events == ["monitor"]
    assert state["sensors"].detect_error == "RuntimeError('no sensors-detect')"
    assert state["checks"] == []
    texts = " | ".join(text for _source, text in lines)
    assert "sensor scan failed" in texts and "cannot start fan control" in texts and "startup checks failed" in texts


async def test_without_scan_and_stopped_checks() -> None:
    """Without the sensor scan (LOAD_SENSOR_MODULES=false) the fans start at once; the checks can be cancelled at the stop."""
    release = threading.Event()
    state: dict[str, Any] = {"checks": []}
    started: list[str] = []

    async def start_fans() -> None:
        started.append("fans")

    await startup(state, lambda: [{"x": release.wait(5)}], None, start_fans, lambda: None, lambda s, t: None)
    assert started == ["fans"] and "sensors" not in state
    await cancel_and_wait(state["checks_task"])
    release.set()
