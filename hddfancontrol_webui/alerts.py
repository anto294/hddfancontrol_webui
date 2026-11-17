"""Keep the list of active alerts, log them and send a notification on each change.

The monitor hands over every RECORD_PERIOD the conditions that hold now, as
{key: (kind, params)}. A key that was not there before raises an alert; a
key that is gone ends it. Each change is logged (in English) and, when an
address is set, sent in the language of the settings, with the texts of the
web UI (static/i18n, keys alert.<kind>).

An alert can be taken note of: the web UI then hides it. The note is kept
in the database (table alert_notes) with the signature of the alert (its
params without the values that change at every sample), so the alert shows
again when it gets worse (a new SMART error). A note is deleted when its
alert ends (a drive or fan that is gone ends its alerts), so the same
problem coming back is shown again.

Notifications go through a queue with one sender task, in order: a slow or
dead server never holds up the monitor that raises the alerts.

An alert still on, and not taken note of, is sent again every
notify_repeat_h hours. In the quiet hours (the minutes the notify_quiet
cron expression matches) only the urgent kinds (URGENT_KINDS) are sent;
the rest waits, and goes out as one message when the quiet hours end.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import cache
from pathlib import Path
from typing import Any

from .aio import cancel_and_wait
from .cron import parse
from .history import History
from .notify import NotifyError, send
from .settings import Settings

I18N_DIR = Path(__file__).parent / "static" / "i18n"

KIND_DRIVE_HOT = "drive_hot"
KIND_FAN_STALLED = "fan_stalled"
KIND_SMART_FAILING = "smart_failing"
KIND_SMART_WARNING = "smart_warning"
KIND_SAFETY = "safety"
KIND_FANS_MISSING = "fans_missing"
KIND_DRIVE_MISSING = "drive_missing"
KIND_SMART_ERROR = "smart_error"
KIND_FAN_WEAR = "fan_wear"
KINDS = (
    KIND_DRIVE_HOT,
    KIND_FAN_STALLED,
    KIND_SMART_FAILING,
    KIND_SMART_WARNING,
    KIND_SAFETY,
    KIND_FANS_MISSING,
    KIND_DRIVE_MISSING,
    KIND_SMART_ERROR,
    KIND_FAN_WEAR,
)

# Params that change at every sample: they are not part of what was taken note of.
VOLATILE_PARAMS = ("temp", "percent")
# Warnings of the web UI itself, not alerts, that can be taken note of too.
BANNER_KEYS = ("banner:no_auth",)
# Messages waiting to be sent; more than this while the server is down are dropped.
QUEUE_SIZE = 100
# The starts of alerts are kept this long, for a summary (which covers at
# most MAX_SUMMARY_DAYS of monitor.py), in the meta table under RAISED_KEY.
RAISED_KEEP_S = 32 * 86400
RAISED_KEY = "alerts_raised"
# Alerts sent even in the quiet hours: something needs a hand now.
URGENT_KINDS = (KIND_DRIVE_HOT, KIND_SAFETY, KIND_SMART_FAILING, KIND_FANS_MISSING, KIND_DRIVE_MISSING)
# At most this many held messages go in the message sent after the quiet hours.
HELD_LINES = 20

Condition = tuple[str, dict[str, Any]]
Sender = Callable[[str, str, str, bool], Awaitable[None]]
Message = tuple[str, str, str, bool]


# ---------------------------
#   texts
# ---------------------------
@cache
def texts(lang: str) -> dict[str, str]:
    """The texts of the web UI in lang, over the English ones."""
    english: dict[str, str] = json.loads((I18N_DIR / "en.json").read_text(encoding="utf-8"))
    if lang == "en":
        return english
    other: dict[str, str] = json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    return {**english, **other}


# ---------------------------
#   render
# ---------------------------
def render(lang: str, key: str, params: dict[str, Any]) -> str:
    """Fill the text of key in lang with params; SMART warning codes become {problems}."""
    strings = texts(lang)
    text = strings.get(key, key)
    if "warnings" in params:
        problems = " ".join(strings.get(f"smart.warn.{code}", code) for code in params["warnings"])
        text = text.replace("{problems}", problems)
    for name, value in params.items():
        text = text.replace("{" + name + "}", str(value))
    return text


# ---------------------------
#   in_quiet_hours
# ---------------------------
def in_quiet_hours(settings: Settings, now: float) -> bool:
    """Whether the minute of now (a timestamp) matches the quiet hours expression, in local time."""
    return bool(settings.notify_quiet) and parse(settings.notify_quiet).matches(now)


# ---------------------------
#   Alerts
# ---------------------------
class Alerts:
    """The alerts in force, and what to do when one starts or ends."""

    def __init__(
        self,
        settings: Callable[[], Settings],
        log: Callable[[str], None],
        sender: Sender = send,
        store: History | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Read the address and language from settings(), log with log, send with sender.

        With store, the notes taken survive a restart in its database.
        """
        self._settings = settings
        self._log = log
        self._send = sender
        self._store = store
        self.active: dict[str, dict[str, Any]] = {}
        # key -> signature of the alert when it was taken note of.
        self.acks: dict[str, str] = store.notes() if store is not None else {}
        self._queue: asyncio.Queue[Message] = asyncio.Queue(QUEUE_SIZE)
        self._worker: asyncio.Task[None] | None = None
        self._clock = clock
        # Texts held during the quiet hours.
        self._held: list[str] = []
        # When each kind of alert started, for the summary: kept RAISED_KEEP_S, in the store across restarts.
        self.raised: list[tuple[float, str]] = []
        if store is not None:
            with suppress(ValueError, TypeError):
                self.raised = [(float(ts), str(kind)) for ts, kind in json.loads(store.get_meta(RAISED_KEY) or "[]")]

    @staticmethod
    def signature(params: dict[str, Any]) -> str:
        """What an alert is about, without the values that change at every sample."""
        return json.dumps({k: v for k, v in params.items() if k not in VOLATILE_PARAMS}, sort_keys=True)

    def ack(self, key: str) -> bool:
        """Take note of an active alert, or of a warning of the web UI. Blocking. False if unknown."""
        if key in self.active:
            signature = self.signature(self.active[key]["params"])
        elif key in BANNER_KEYS:
            signature = ""
        else:
            return False
        self.acks[key] = signature
        if self._store is not None:
            self._store.save_note(key, signature)
        return True

    def unack(self, key: str) -> None:
        """Show again what was taken note of. Blocking."""
        if self.acks.pop(key, None) is not None and self._store is not None:
            self._store.delete_notes([key])

    async def update(self, conditions: dict[str, Condition]) -> None:
        """Raise the alerts that start now, remind of those still on, and end those that are over."""
        now = self._clock()
        settings = self._settings()
        count = len(self.raised)
        for key, (kind, params) in conditions.items():
            if key in self.active:
                self.active[key]["params"] = params
                continue
            self.active[key] = {"kind": kind, "params": params, "since": now, "sent": now}
            self.raised.append((now, kind))
            self._log("ALERT: " + render("en", f"alert.{kind}", params))
            self._notify(settings, kind, render(settings.notify_language, f"alert.{kind}", params), urgent=True)
        # A note lasts while the alert does: the next time it starts, it shows again.
        dropped = [k for k in self.acks if k not in BANNER_KEYS and k not in conditions]
        for key in dropped:
            del self.acks[key]
        if dropped and self._store is not None:
            await asyncio.to_thread(self._store.delete_notes, dropped)
        for key in [k for k in self.active if k not in conditions]:
            alert = self.active.pop(key)
            self._log("Over: " + render("en", f"alert.{alert['kind']}", alert["params"]))
            text = render(settings.notify_language, f"alert.{alert['kind']}", alert["params"])
            self._notify(settings, alert["kind"], render(settings.notify_language, "alert.over", {"text": text}), urgent=False)
        self._remind(settings, now)
        if self._held and not in_quiet_hours(settings, now):
            self._send_held(settings)
        # A summary covers MAX_SUMMARY_DAYS at most.
        kept = [(ts, kind) for ts, kind in self.raised if now - ts < RAISED_KEEP_S]
        changed = len(self.raised) != count or len(kept) != len(self.raised)
        self.raised = kept
        if changed and self._store is not None:
            await asyncio.to_thread(self._store.set_meta, RAISED_KEY, json.dumps(self.raised))

    def _remind(self, settings: Settings, now: float) -> None:
        """Send again the alerts still on after notify_repeat_h hours, unless taken note of."""
        if settings.notify_repeat_h <= 0:
            return
        for key, alert in self.active.items():
            if self.acks.get(key) == self.signature(alert["params"]):
                continue
            if now - alert["sent"] < settings.notify_repeat_h * 3600:
                continue
            # A reminder of something not urgent waits for the end of the quiet hours.
            if alert["kind"] not in URGENT_KINDS and in_quiet_hours(settings, now):
                continue
            alert["sent"] = now
            text = render(settings.notify_language, f"alert.{alert['kind']}", alert["params"])
            hours = f"{(now - alert['since']) / 3600:.0f}"
            self._notify(
                settings,
                alert["kind"],
                render(settings.notify_language, "alert.still", {"text": text, "hours": hours}),
                urgent=True,
            )

    def _notify(self, settings: Settings, kind: str, text: str, urgent: bool) -> None:
        """Queue one message; in the quiet hours, hold it unless it is an urgent start. Never waits for the server."""
        if not settings.notify_url:
            return
        if in_quiet_hours(settings, self._clock()) and not (urgent and kind in URGENT_KINDS):
            self._held.append(text)
            return
        self._queue_message(settings.notify_url, settings.notify_format, text, urgent)

    def _send_held(self, settings: Settings) -> None:
        """Send what the quiet hours held, as one message."""
        held, self._held = self._held, []
        lines = held[:HELD_LINES] + (["…"] if len(held) > HELD_LINES else [])
        text = render(settings.notify_language, "alert.held", {"count": len(held)}) + "\n" + "\n".join(lines)
        if settings.notify_url:
            self._queue_message(settings.notify_url, settings.notify_format, text, False)

    def send_summary(self, text: str) -> None:
        """Send a summary (held in the quiet hours like any message that is not urgent)."""
        self._notify(self._settings(), "summary", text, urgent=False)

    def _queue_message(self, url: str, fmt: str, text: str, urgent: bool) -> None:
        """Put a message in the queue of the sender, starting it if needed."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._send_queued(), name="notify")
        try:
            self._queue.put_nowait((url, fmt, text, urgent))
        except asyncio.QueueFull:
            self._log("WARNING: too many notifications waiting, one is dropped")

    async def _send_queued(self) -> None:
        """Send the queued messages one after the other; a failure is logged, never raised."""
        while True:
            url, fmt, text, urgent = await self._queue.get()
            try:
                await self._send(url, fmt, text, urgent)
            except NotifyError as err:
                self._log(f"WARNING: cannot send the notification: {err}")
            except Exception as err:  # the sender must go on
                self._log(f"WARNING: cannot send the notification: {err!r}")
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        """Wait until every queued message was sent or failed."""
        await self._queue.join()

    async def stop(self) -> None:
        """Stop the sender; messages still queued are dropped."""
        if self._worker is not None:
            await cancel_and_wait(self._worker)
            self._worker = None

    async def test(self) -> None:
        """Send a test message with the settings in force. Raise NotifyError."""
        settings = self._settings()
        if not settings.notify_url:
            raise NotifyError("no address")
        await self._send(
            settings.notify_url, settings.notify_format, render(settings.notify_language, "alert.test", {}), False
        )

    def status(self) -> list[dict[str, Any]]:
        """The alerts in force, oldest first, as JSON-ready data."""
        return [
            {"key": key, **alert, "acknowledged": self.acks.get(key) == self.signature(alert["params"])}
            for key, alert in sorted(self.active.items(), key=lambda item: item[1]["since"])
        ]
