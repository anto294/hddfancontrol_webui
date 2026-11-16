"""Feed the history database and refresh the SMART data in the background."""

from __future__ import annotations

import asyncio
import os
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
# The temperature line is logged when a value changes, and at least this often.
TEMPS_HEARTBEAT = 600.0

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
        log: Callable[[str], None] | None = None,
    ) -> None:
        """Read from these sources and write to history; log the temperatures with log."""
        self.history = history
        self.temp_log = temp_log
        self.hardware = hardware
        self._settings = settings
        self._smart_reader = smart_reader
        self._log = log
        self._last_sample: str | None = None
        # PWM (%) and speed (RPM) of every output, as read by the last record_once.
        self._fans: dict[str, tuple[float | None, int | None]] = {}
        self._logged_key: tuple[Any, ...] | None = None
        self._logged_at = 0.0
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

    def _stable_names(self) -> dict[str, str]:
        """Map the kernel name of each drive (sda) to one of its stable names.

        sdX can change at every boot, so the history keeps a drive under a
        /dev/disk/by-id path: its wwn- name when it has one, else the first in
        order, the same whichever name the settings use.
        """
        names = {}
        for drive in self.hardware.drives():
            if drive.ids:
                names[drive.name] = min(drive.ids, key=lambda i: (not os.path.basename(i).startswith("wwn-"), i))
        return names

    def record_once(self) -> int:
        """Store the newest temperatures, if new, and the fan speeds. Return how many values."""
        values: list[tuple[str, str, float]] = []
        latest = self.temp_log.latest()
        if latest is not None and latest["time"] != self._last_sample:
            self._last_sample = latest["time"]
            ts = int(datetime.fromisoformat(latest["time"]).timestamp())
            # hddfancontrol names a drive "sda MODEL": keep it under a name that survives a reboot.
            stable = self._stable_names()
            values += [
                ("temp", stable.get(name.split()[0], name), temp) for name, temp in latest["temps"].items() if temp is not None
            ]
            if values:
                self.history.record(ts, values)
        fans: list[tuple[str, str, float]] = []
        self._fans = {}
        for device in self.hardware.hwmons():
            for pwm in device.pwms:
                self._fans[pwm.path] = (None if pwm.value is None else pwm.value * 100 / 255, pwm.rpm)
                if pwm.value is not None:
                    fans.append(("pwm", pwm.path, round(pwm.value * 100 / 255, 1)))
                if pwm.rpm is not None:
                    fans.append(("rpm", pwm.path, float(pwm.rpm)))
        if fans:
            self.history.record(int(time.time()), fans)
        return len(values) + len(fans)

    def summary(self) -> tuple[tuple[Any, ...], str] | None:
        """Describe the drives, the extra sensors and the chosen fans in one line. Blocking.

        Returns what the line depends on, rounded so that noise does not count
        as a change, and the line; None when there is nothing to describe.
        """
        settings = self._settings()
        parts: list[str] = []
        key: list[Any] = []
        latest = self.temp_log.latest()
        if latest is not None and settings.drives:
            temps = list(latest["temps"].items())
            split = len(temps) - len(settings.hwmons)
            drives = [(name.split()[0], temp) for name, temp in temps[:split]]
            key += [(name, None if temp is None else round(temp)) for name, temp in drives]
            text = "controlled: " + ", ".join(
                f"{name} {temp:g}°C" if temp is not None else f"{name} asleep" for name, temp in drives
            )
            awake = [(temp, name) for name, temp in drives if temp is not None]
            if awake:
                temp, name = max(awake)
                text += f" (hottest {name} {temp:g}°C)"
            parts.append(text)
            sensors = [(name, temp) for name, temp in temps[split:] if temp is not None]
            if sensors:
                key += [(name, round(temp)) for name, temp in sensors]
                parts.append("sensors: " + ", ".join(f"{name} {temp:g}°C" for name, temp in sensors))
        fans = []
        for pwm in settings.pwms:
            percent, rpm = self._fans.get(pwm.path, (None, None))
            key.append((pwm.path, None if percent is None else round(percent)))
            speed = "?" if percent is None else f"{percent:.0f}%"
            fans.append(f"{os.path.basename(pwm.path)} {speed}" + ("" if rpm is None else f" {rpm} RPM"))
        if fans:
            parts.append("fans: " + ", ".join(fans))
        # Drives that are only monitored have no sample of hddfancontrol: their
        # temperature is the one of the last SMART read.
        only_monitored = [d for d in settings.monitor_drives if d not in settings.drives]
        if only_monitored:
            smart = self.history.smart()
            items = []
            for device in only_monitored:
                name = os.path.basename(os.path.realpath(device))
                temp = smart.get(device, {}).get("temperature")
                key.append((name, temp))
                items.append(f"{name} {temp}°C" if temp is not None else f"{name} ?")
            parts.append("monitored (SMART): " + ", ".join(items))
        if not parts:
            return None
        return tuple(key), " | ".join(parts)

    def _log_summary(self, summary: tuple[tuple[Any, ...], str] | None) -> None:
        """Log the line when it changed, or when the last one is TEMPS_HEARTBEAT old."""
        if self._log is None or summary is None:
            return
        key, line = summary
        now = time.monotonic()
        if key != self._logged_key or now - self._logged_at >= TEMPS_HEARTBEAT:
            self._log(line)
            self._logged_key = key
            self._logged_at = now

    async def _record_loop(self) -> None:
        """Record every RECORD_PERIOD seconds, log the temperatures and purge every hour."""
        last_purge = 0.0
        while True:
            await asyncio.to_thread(self.record_once)
            self._log_summary(await asyncio.to_thread(self.summary))
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
