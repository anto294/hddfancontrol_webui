"""Keep the list of active alerts, log them and send a notification on each change.

The monitor hands over every RECORD_PERIOD the conditions that hold now, as
{key: (kind, params)}. A key that was not there before raises an alert; a
key that is gone ends it. Each change is logged (in English) and, when an
address is set, sent in the language of the settings, with the texts of the
web UI (static/i18n, keys alert.<kind>).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Any

from .notify import NotifyError, send
from .settings import Settings

I18N_DIR = Path(__file__).parent / "static" / "i18n"

KIND_DRIVE_HOT = "drive_hot"
KIND_FAN_STALLED = "fan_stalled"
KIND_SMART_FAILING = "smart_failing"
KIND_SMART_WARNING = "smart_warning"
KIND_SAFETY = "safety"
KIND_FANS_MISSING = "fans_missing"
KINDS = (KIND_DRIVE_HOT, KIND_FAN_STALLED, KIND_SMART_FAILING, KIND_SMART_WARNING, KIND_SAFETY, KIND_FANS_MISSING)

Condition = tuple[str, dict[str, Any]]
Sender = Callable[[str, str, str, bool], Awaitable[None]]


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
    """Fill the text of key in lang with params."""
    text = texts(lang).get(key, key)
    for name, value in params.items():
        text = text.replace("{" + name + "}", str(value))
    return text


# ---------------------------
#   Alerts
# ---------------------------
class Alerts:
    """The alerts in force, and what to do when one starts or ends."""

    def __init__(self, settings: Callable[[], Settings], log: Callable[[str], None], sender: Sender = send) -> None:
        """Read the address and language from settings(), log with log, send with sender."""
        self._settings = settings
        self._log = log
        self._send = sender
        self.active: dict[str, dict[str, Any]] = {}

    async def update(self, conditions: dict[str, Condition]) -> None:
        """Raise the alerts that start now and end those that are over."""
        for key, (kind, params) in conditions.items():
            if key in self.active:
                self.active[key]["params"] = params
                continue
            self.active[key] = {"kind": kind, "params": params, "since": time.time()}
            self._log("ALERT: " + render("en", f"alert.{kind}", params))
            await self._notify(f"alert.{kind}", params, urgent=True)
        for key in [k for k in self.active if k not in conditions]:
            alert = self.active.pop(key)
            self._log("Over: " + render("en", f"alert.{alert['kind']}", alert["params"]))
            await self._notify(f"alert.{alert['kind']}", alert["params"], urgent=False)

    async def _notify(self, key: str, params: dict[str, Any], urgent: bool) -> None:
        """Send one alert, or its end; a failure is logged, never raised."""
        settings = self._settings()
        if not settings.notify_url:
            return
        text = render(settings.notify_language, key, params)
        if not urgent:
            text = render(settings.notify_language, "alert.over", {"text": text})
        try:
            await self._send(settings.notify_url, settings.notify_format, text, urgent)
        except NotifyError as err:
            self._log(f"WARNING: cannot send the notification: {err}")

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
        return [{"key": key, **alert} for key, alert in sorted(self.active.items(), key=lambda item: item[1]["since"])]
