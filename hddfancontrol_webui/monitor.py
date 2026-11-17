"""Feed the history database and refresh the SMART data in the background."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .aio import cancel_and_wait
from .alerts import (
    KIND_DRIVE_HOT,
    KIND_DRIVE_MISSING,
    KIND_FAN_STALLED,
    KIND_FAN_WEAR,
    KIND_FANS_MISSING,
    KIND_SAFETY,
    KIND_SMART_ERROR,
    KIND_SMART_FAILING,
    KIND_SMART_WARNING,
    Alerts,
    Condition,
    render,
)
from .const import RECORD_PERIOD
from .cron import parse
from .hardware import Hardware
from .history import History
from .settings import Settings
from .smart import STATUS_ERROR, STATUS_STANDBY, failed_now, read_smart, start_selftest
from .templog import TempLog

PURGE_PERIOD = 3600.0
# The temperature line is logged when a value changes, and at least this often.
TEMPS_HEARTBEAT = 600.0
# A chosen fan that turned once and then shows 0 RPM this long, with a PWM
# value well over its stop value, is reported as stalled.
FAN_STALL_SECONDS = 120.0
# A hot drive alert ends this much under the maximum, so that it does not
# start and end at every sample around the maximum.
HOT_HYSTERESIS = 2.0
# SMART counters that only grow: a higher value is a new problem.
SMART_COUNTS = (
    "reallocated_sectors",
    "pending_sectors",
    "offline_uncorrectable",
    "reported_uncorrectable",
    "crc_errors",
    "media_errors",
)
# Drive temperatures older than this say nothing about now (the safety
# alert of the fan controller covers them).
FRESH_SAMPLE_SECONDS = 600.0
# The meta key of the last summary sent, as "<expression>|<timestamp>". A
# summary covers the time since the run before (at most MAX_SUMMARY_DAYS).
SUMMARY_KEY = "summary_last"
MAX_SUMMARY_DAYS = 31

# A self-test due (by its cron expression) is tried for this long: a drive
# asleep is tried again every minute, and a web UI started late still runs it.
SELFTEST_GRACE = 3 * 3600
# No test starts on a drive whose long test started less than this ago: it would abort it.
LONG_TEST_GUARD = 86400

# Fan wear: the RPM of a chosen fan at the same PWM, over the last WEAR_WINDOW,
# against a baseline of its first WEAR_WINDOW of history, once the history is
# WEAR_BASELINE_AGE old. The baseline is kept, so a slow drop over months
# still shows. A drop of WEAR_DROP or more raises the fan_wear alert.
WEAR_WINDOW = 7 * 86400
WEAR_BASELINE_AGE = 21 * 86400
WEAR_DROP = 0.15
WEAR_MIN_SAMPLES = 30
WEAR_MIN_RPM = 300.0
WEAR_PERIOD = 86400.0

# How often the SMART loop looks at its interval.
SMART_CHECK_PERIOD = 60.0

_LOGGER = logging.getLogger(__name__)

SmartReader = Callable[[str], Awaitable[dict[str, Any]]]
SelfTest = Callable[[str, str], Awaitable[dict[str, Any]]]


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
        alerts: Alerts | None = None,
        status: Callable[[], dict[str, Any]] | None = None,
        labels: Callable[[], dict[str, str]] | None = None,
        selftest: SelfTest = start_selftest,
    ) -> None:
        """Read from these sources and write to history; log the temperatures with log.

        With alerts, the conditions of conditions() are handed to it after
        each record; status() is the status of the supervisor and labels()
        the names given to drives and fans.
        """
        self.history = history
        self.temp_log = temp_log
        self.hardware = hardware
        self._settings = settings
        self._smart_reader = smart_reader
        self._selftest = selftest
        self._log = log
        self.alerts = alerts
        self._status = status
        self._labels = labels or dict
        # Fans seen turning (so they have a speed sensor), and since when a fan shows 0 RPM.
        self._spun: set[str] = set()
        self._stall_since: dict[str, float] = {}
        self._last_sample: str | None = None
        # PWM (%) and speed (RPM) of every output, as read by the last record_once.
        self._fans: dict[str, tuple[float | None, int | None]] = {}
        self._logged_key: tuple[Any, ...] | None = None
        self._logged_at = 0.0
        self._tasks: list[asyncio.Task[None]] = []
        self._smart_lock = asyncio.Lock()
        # Fans turning slower than their baseline: {path: drop}, checked daily.
        self.wear: dict[str, float] = {}
        self._wear_at = -WEAR_PERIOD
        # The last error of each loop, logged once until the loop works again.
        self._errors: dict[str, str] = {}

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

    def _failed(self, loop: str, err: Exception) -> None:
        """Log an error of a loop, once until it changes or the loop works again."""
        text = f"{type(err).__name__}: {err}"
        if self._errors.get(loop) != text:
            self._errors[loop] = text
            _LOGGER.error("The %s loop failed", loop, exc_info=err)
            if self._log is not None:
                self._log(f"ERROR: {loop} failed, trying again: {text}")

    def _worked(self, loop: str) -> None:
        """A loop worked: say so if it had failed."""
        if self._errors.pop(loop, None) is not None and self._log is not None:
            self._log(f"{loop} works again")

    async def _record_loop(self) -> None:
        """Record every RECORD_PERIOD seconds, log the temperatures, send the summary, check fan wear and purge.

        Each job has its own guard: an error (a full disk, a locked
        database, a bad value in meta) is logged once and the loop goes on,
        and one failing job never keeps the others from running. The alerts
        must never stop for good, and the purge must not stop either, or
        history.db grows without limit.
        """
        last_purge = 0.0
        while True:
            try:
                await self._record_step()
                self._worked("recording")
            except Exception as err:  # the loop must outlive any error
                self._failed("recording", err)
            try:
                await self._send_summary()
                self._worked("summary")
            except Exception as err:  # the loop must outlive any error
                self._failed("summary", err)
            if time.monotonic() - self._wear_at >= WEAR_PERIOD:
                # Set first: a check that fails waits for the next period too.
                self._wear_at = time.monotonic()
                try:
                    self.wear = await asyncio.to_thread(self.fan_wear)
                    self._worked("fan wear check")
                except Exception as err:  # the loop must outlive any error
                    self._failed("fan wear check", err)
            if time.monotonic() - last_purge >= PURGE_PERIOD:
                last_purge = time.monotonic()
                try:
                    settings = self._settings()
                    await asyncio.to_thread(self.history.purge, settings.history_days)
                    # SMART data of a drive no longer monitored (or gone) is not shown: drop it.
                    await asyncio.to_thread(self.history.forget_smart, set(settings.monitor_drives))
                    self._worked("purge")
                except Exception as err:  # the loop must outlive any error
                    self._failed("purge", err)
            await asyncio.sleep(RECORD_PERIOD)

    async def _record_step(self) -> None:
        """Record once, log the temperatures and update the alerts; the alerts run even if recording fails."""
        try:
            await asyncio.to_thread(self.record_once)
            self._log_summary(await asyncio.to_thread(self.summary))
        finally:
            if self.alerts is not None:
                status = self._status() if self._status is not None else {}
                await self.alerts.update(await asyncio.to_thread(self.conditions, status))

    def fan_wear(self, now: float | None = None) -> dict[str, float]:
        """How much slower each chosen fan turns than at first, at the same PWM: {path: drop} for drops of WEAR_DROP. Blocking."""
        now = time.time() if now is None else now
        oldest = self.history.oldest()
        worn: dict[str, float] = {}
        for pwm in self._settings().pwms:
            key = f"fan_baseline_{pwm.path}"
            saved = self.history.get_meta(key)
            if saved is None:
                if oldest is None or now - oldest < WEAR_BASELINE_AGE:
                    continue
                base = self.history.fan_curve(pwm.path, oldest, oldest + WEAR_WINDOW)
                if not base:
                    continue
                self.history.set_meta(key, json.dumps({str(band): value for band, value in base.items()}))
            else:
                base = {float(band): (rpm, count) for band, (rpm, count) in json.loads(saved).items()}
            recent = self.history.fan_curve(pwm.path, int(now - WEAR_WINDOW), int(now))
            weight = ratio = 0.0
            for band, (rpm, count) in recent.items():
                if band not in base:
                    continue
                base_rpm, base_count = base[band]
                if min(count, base_count) < WEAR_MIN_SAMPLES or base_rpm < WEAR_MIN_RPM:
                    continue
                share = min(count, base_count)
                ratio += share * rpm / base_rpm
                weight += share
            if weight and 1 - ratio / weight >= WEAR_DROP:
                worn[pwm.path] = 1 - ratio / weight
        return worn

    def summary_due(self, now: float | None = None) -> float | None:
        """The time of the summary to send now, or None. Blocking: SQLite.

        The first time (or after the expression changed) nothing is sent:
        the time is only recorded, and the first summary comes at the next run.
        """
        settings = self._settings()
        if not settings.notify_summary or not settings.notify_url:
            return None
        now = time.time() if now is None else now
        due = parse(settings.notify_summary).previous(now)
        if due is None:
            return None
        expression, _, last = (self.history.get_meta(SUMMARY_KEY) or "").rpartition("|")
        if expression != settings.notify_summary:
            self.history.set_meta(SUMMARY_KEY, f"{settings.notify_summary}|{due}")
            return None
        return due if due > float(last) else None

    def build_summary(self, due: float) -> str:
        """The text of the summary that ends at due, in the language of the alerts. Blocking: files and SQLite."""
        settings = self._settings()
        lang = settings.notify_language
        before = parse(settings.notify_summary).previous(due - 60)
        since = int(max(before or 0, due - MAX_SUMMARY_DAYS * 86400))
        span = {name: time.strftime("%d/%m %H:%M", time.localtime(stamp)) for name, stamp in (("from", since), ("to", due))}
        lines = [render(lang, "summary.title", span)]
        drives = self.hardware.drives()
        by_id = {i: drive for drive in drives for i in drive.ids}
        stats = self.history.stats(since)
        for kind, name, _low, avg, high in stats:
            if kind != "temp":
                continue
            drive = by_id.get(name)
            fallback = drive.model if drive else name.split()[-1] if not name.startswith("/") else os.path.basename(name)
            label = self._drive_name(drive.ids if drive else [name], fallback)
            lines.append(render(lang, "summary.drive", {"drive": label, "max": f"{high:.0f}", "avg": f"{avg:.0f}"}))
        chosen = {pwm.path for pwm in settings.pwms}
        for kind, name, _low, avg, _high in stats:
            if kind == "rpm" and name in chosen:
                lines.append(render(lang, "summary.fan", {"fan": self._fan_name(name), "rpm": f"{avg:.0f}"}))
        if self.alerts is not None:
            raised = sum(1 for ts, _ in self.alerts.raised if ts >= since)
            lines.append(render(lang, "summary.alerts", {"count": raised, "active": len(self.alerts.active)}))
        smart = [data.get("status") for data in self.smart().values()]
        if smart:
            lines.append(
                render(
                    lang,
                    "summary.smart",
                    {"ok": smart.count("ok"), "warning": smart.count("warning"), "failing": smart.count("failing")},
                )
            )
        return "\n".join(lines)

    async def _send_summary(self) -> None:
        """Send the summary when one is due."""
        if self.alerts is None:
            return
        due = await asyncio.to_thread(self.summary_due)
        if due is None:
            return
        text = await asyncio.to_thread(self.build_summary, due)
        self.alerts.send_summary(text)
        await asyncio.to_thread(self.history.set_meta, SUMMARY_KEY, f"{self._settings().notify_summary}|{due}")

    def _drive_name(self, ids: list[str], fallback: str) -> str:
        """The name given to a drive, by any of its stable names, or fallback."""
        labels = self._labels()
        return next((labels[i] for i in ids if i in labels), fallback)

    def _fan_name(self, path: str) -> str:
        """The name given to a fan, or "nct6798 pwm1"."""
        label = self._labels().get(path)
        if label:
            return label
        for device in self.hardware.hwmons():
            if any(pwm.path == path for pwm in device.pwms):
                return f"{device.name or device.hwmon} {os.path.basename(path)}"
        return os.path.basename(path)

    def conditions(self, status: dict[str, Any], now: float | None = None) -> dict[str, Condition]:
        """What is wrong now, as {key: (kind, params)}. Blocking: files and SQLite.

        status is the status of the supervisor. Uses the fan readings of the
        last record_once.
        """
        now = time.monotonic() if now is None else now
        settings = self._settings()
        active = self.alerts.active if self.alerts is not None else {}
        found: dict[str, Condition] = {}
        drives = {drive.name: drive for drive in self.hardware.drives()}

        latest = self.temp_log.latest()
        fresh = latest is not None and time.time() - datetime.fromisoformat(latest["time"]).timestamp() < FRESH_SAMPLE_SECONDS
        if latest is not None and fresh and settings.drives:
            temps = list(latest["temps"].items())
            # The drive's own maximum, found as the fan controller finds it (by-id, by-path or /dev/sdX).
            names = self.hardware.kernel_names()
            by_kernel = {names[d]: d for d in settings.drives if d in names}
            for name, temp in temps[: len(temps) - len(settings.hwmons)]:
                kernel = name.split()[0]
                key = f"hot:{kernel}"
                drive = drives.get(kernel)
                maximum = settings.limits_of(by_kernel.get(kernel, ""))[2]
                limit = maximum - (HOT_HYSTERESIS if key in active else 0.0)
                if temp is not None and temp >= limit:
                    label = self._drive_name(drive.ids if drive else [], name)
                    found[key] = (KIND_DRIVE_HOT, {"drive": label, "temp": f"{temp:g}", "max": f"{maximum:g}"})

        mode = status.get("mode")
        for pwm in settings.pwms:
            percent, rpm = self._fans.get(pwm.path, (None, None))
            if rpm:
                self._spun.add(pwm.path)
            # Halfway between stop and start, the fan turns for sure.
            threshold = (pwm.start + pwm.stop) / 2 * 100 / 255
            watching = mode not in ("pwm_test", "read_only") and pwm.path in self._spun
            if watching and rpm == 0 and percent is not None and percent >= threshold:
                since = self._stall_since.setdefault(pwm.path, now)
                if now - since >= FAN_STALL_SECONDS:
                    found[f"stalled:{pwm.path}"] = (
                        KIND_FAN_STALLED,
                        {"fan": self._fan_name(pwm.path), "percent": f"{percent:.0f}"},
                    )
            else:
                self._stall_since.pop(pwm.path, None)

        chosen = {pwm.path for pwm in settings.pwms}
        for path, drop in self.wear.items():
            if path in chosen:
                found[f"wear:{path}"] = (KIND_FAN_WEAR, {"fan": self._fan_name(path), "percent": f"{drop * 100:.0f}"})

        # The chip of a chosen fan is gone (a kernel update can rename it): those fans are not controlled.
        if status.get("missing_pwms"):
            found["fans_missing"] = (
                KIND_FANS_MISSING,
                {"fans": ", ".join(self._fan_name(path) for path in status["missing_pwms"])},
            )

        fans = status.get("fans", {})
        if fans.get("safety") or fans.get("error") or fans.get("auto", {}).get("failsafe"):
            found["safety"] = (KIND_SAFETY, {})

        by_id = {i: drive for drive in drives.values() for i in drive.ids}
        # A monitored drive that is not on the host any more (unplugged, dead,
        # dropped by its controller). Without any drive listed, /dev is not
        # readable here: that says nothing about the drives.
        missing = set()
        if drives:
            for device in settings.monitor_drives:
                if not self.hardware.dev_exists(device):
                    missing.add(device)
                    name = self._drive_name([device], os.path.basename(device))
                    found[f"missing:{device}"] = (KIND_DRIVE_MISSING, {"drive": name})
        for device, data in self.smart().items():
            if device in missing:
                continue
            drive = by_id.get(device)
            if data.get("status") == STATUS_ERROR:
                fallback = f"{drive.model} ({drive.name})" if drive else os.path.basename(device)
                messages = data.get("messages") or []
                found[f"smart_error:{device}"] = (
                    KIND_SMART_ERROR,
                    {
                        "drive": self._drive_name(drive.ids if drive else [device], fallback),
                        "error": "; ".join(messages)[:200],
                    },
                )
                continue
            kind = {"failing": KIND_SMART_FAILING, "warning": KIND_SMART_WARNING}.get(data.get("status", ""))
            if kind is None:
                continue
            fallback = f"{drive.model} ({drive.name})" if drive else os.path.basename(device)
            # The counts make a new error show again an alert that was taken note of.
            counts = {field: data[field] for field in SMART_COUNTS if data.get(field)}
            counts["failed"] = sorted(a.get("id") for a in data.get("attributes", []) if failed_now(a.get("failed")))
            params = {
                "drive": self._drive_name(drive.ids if drive else [device], fallback),
                "warnings": data.get("warnings", []),
            }
            found[f"smart:{device}"] = (kind, {**params, "counts": counts})
        return found

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

    async def run_selftests(self, now: float | None = None) -> list[tuple[str, str]]:
        """Start the self-tests that are due by their cron expressions; return (device, kind) of those started.

        A test is due from the last run of its expression for SELFTEST_GRACE,
        once per drive. A drive asleep is left alone and tried again a
        minute later. No test starts on a drive whose long test started less
        than LONG_TEST_GUARD ago (it would abort it); the long test goes first.
        """
        settings = self._settings()
        now = time.time() if now is None else now
        started: list[tuple[str, str]] = []
        for kind, expression in (("long", settings.selftest_long), ("short", settings.selftest_short)):
            if not expression:
                continue
            due = parse(expression).previous(now)
            if due is None or now - due > SELFTEST_GRACE:
                continue
            for device in settings.monitor_drives:
                key = f"selftest_{kind}_{device}"
                last = await asyncio.to_thread(self.history.get_meta, key)
                if last is not None and float(last) >= due:
                    continue
                long_last = await asyncio.to_thread(self.history.get_meta, f"selftest_long_started_{device}")
                if long_last is not None and now - float(long_last) < LONG_TEST_GUARD:
                    continue
                result = await self._selftest(device, kind)
                if result.get("status") == STATUS_STANDBY:
                    continue
                await asyncio.to_thread(self.history.set_meta, key, str(now))
                name = os.path.basename(device)
                if result.get("status") == "started":
                    started.append((device, kind))
                    if kind == "long":
                        await asyncio.to_thread(self.history.set_meta, f"selftest_long_started_{device}", str(now))
                    if self._log is not None:
                        self._log(f"SMART {kind} self-test started on {name}")
                elif self._log is not None:
                    # Not tried again before the next run: a drive that cannot test would be tried for hours.
                    self._log(
                        f"WARNING: cannot start the SMART {kind} self-test on {name}: {'; '.join(result.get('messages', []))}"
                    )
        return started

    async def _smart_loop(self) -> None:
        """Read the SMART data at the runs of smart_schedule; empty means only on demand.

        The expression is read again every SMART_CHECK_PERIOD, so a new one
        applies at once. The first read comes SMART_CHECK_PERIOD after the
        start; after it, a read is due when the expression ran since the last.
        """
        last: float | None = None
        while True:
            await asyncio.sleep(SMART_CHECK_PERIOD)
            try:
                await self.run_selftests()
            except Exception as err:  # the loop must outlive any error
                self._failed("SMART self-test", err)
            expression = self._settings().smart_schedule
            now = time.time()
            if not expression:
                continue
            if last is not None:
                due = parse(expression).previous(now)
                if due is None or due <= last:
                    continue
            try:
                await self.refresh_smart()
                self._worked("SMART refresh")
            except Exception as err:  # the loop must outlive any error
                self._failed("SMART refresh", err)
            last = now
