"""Feed the history database and refresh the SMART data in the background."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .aio import cancel_and_wait
from .const import RECORD_PERIOD
from .hardware import Hardware
from .history import History
from .settings import Settings
from .smart import STATUS_STANDBY, read_smart
from .templog import TempLog

PURGE_PERIOD = 3600.0

SmartReader = Callable[[str], Awaitable[dict[str, Any]]]


# ---------------------------
#   Monitor
# ---------------------------
class Monitor:
    """Record temperatures and fan speeds, purge old samples, read SMART data."""

    def __init__(
        self,
        history: History,
        temp_log: TempLog,
        hardware: Hardware,
        settings: Callable[[], Settings],
        smart_reader: SmartReader = read_smart,
    ) -> None:
        """Read from these sources and write to history."""
        self.history = history
        self.temp_log = temp_log
        self.hardware = hardware
        self._settings = settings
        self._smart_reader = smart_reader
        self._last_sample: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._smart_lock = asyncio.Lock()

    def start(self) -> None:
        """Start the background loops."""
        self._tasks = [
            asyncio.create_task(self._record_loop(), name="record"),
            asyncio.create_task(self._smart_loop(), name="smart"),
        ]

    async def stop(self) -> None:
        """Stop the background loops."""
        for task in self._tasks:
            await cancel_and_wait(task)
        self._tasks = []

    def record_once(self) -> int:
        """Store the newest temperatures, if new, and the fan speeds. Return how many values."""
        values: list[tuple[str, str, float]] = []
        latest = self.temp_log.latest()
        if latest is not None and latest["time"] != self._last_sample:
            self._last_sample = latest["time"]
            ts = int(datetime.fromisoformat(latest["time"]).timestamp())
            values += [("temp", name, temp) for name, temp in latest["temps"].items() if temp is not None]
            if values:
                self.history.record(ts, values)
        fans: list[tuple[str, str, float]] = []
        for device in self.hardware.hwmons():
            for pwm in device.pwms:
                if pwm.value is not None:
                    fans.append(("pwm", pwm.path, round(pwm.value * 100 / 255, 1)))
                if pwm.rpm is not None:
                    fans.append(("rpm", pwm.path, float(pwm.rpm)))
        if fans:
            self.history.record(int(time.time()), fans)
        return len(values) + len(fans)

    async def _record_loop(self) -> None:
        """Record every RECORD_PERIOD seconds and purge every hour."""
        last_purge = 0.0
        while True:
            await asyncio.to_thread(self.record_once)
            if time.monotonic() - last_purge >= PURGE_PERIOD:
                await asyncio.to_thread(self.history.purge, self._settings().history_days)
                last_purge = time.monotonic()
            await asyncio.sleep(RECORD_PERIOD)

    def smart(self) -> dict[str, dict[str, Any]]:
        """The last SMART data of the monitored drives. Blocking: SQLite."""
        monitored = self._settings().monitor_drives
        return {device: data for device, data in self.history.smart().items() if device in monitored}

    async def refresh_smart(self) -> dict[str, dict[str, Any]]:
        """Read the monitored drives now. A sleeping drive keeps its previous data, marked as asleep."""
        async with self._smart_lock:
            devices = self._settings().monitor_drives
            previous = await asyncio.to_thread(self.history.smart)
            for device in devices:
                data = await self._smart_reader(device)
                if data.get("status") == STATUS_STANDBY and device in previous:
                    old = dict(previous[device])
                    old.pop("time", None)
                    old["asleep"] = True
                    await asyncio.to_thread(self.history.save_smart, device, old, previous[device]["time"])
                    continue
                await asyncio.to_thread(self.history.save_smart, device, data)
            return await asyncio.to_thread(self.smart)

    async def _smart_loop(self) -> None:
        """Read the SMART data every smart_interval_h hours; 0 means only on demand."""
        await asyncio.sleep(60)
        while True:
            hours = self._settings().smart_interval_h
            if hours > 0:
                await self.refresh_smart()
                await asyncio.sleep(hours * 3600)
            else:
                await asyncio.sleep(600)
