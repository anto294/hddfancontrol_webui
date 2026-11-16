"""Keep the list of active alerts, log them and send a notification on each change.

The monitor hands over every RECORD_PERIOD the conditions that hold now, as
{key: (kind, params)}. A key that was not there before raises an alert; a
key that is gone ends it. Each change is logged (in English) and, when an
address is set, sent in the language of the settings, with the texts of the
web UI (static/i18n, keys alert.<kind>).

An alert can be taken note of: the web UI then hides it. The note is kept
in /config/alerts.json with the signature of the alert (its params without
the values that change at every sample), so the alert shows again when it
gets worse (a new SMART error) or when it ends and comes back.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from functools import cache
from pathlib import Path
from typing import Any

from .notify import NotifyError, send
from .settings import Settings
from .storage import write_atomic

I18N_DIR = Path(__file__).parent / "static" / "i18n"

KIND_DRIVE_HOT = "drive_hot"
KIND_FAN_STALLED = "fan_stalled"
KIND_SMART_FAILING = "smart_failing"
KIND_SMART_WARNING = "smart_warning"
KIND_SAFETY = "safety"
KIND_FANS_MISSING = "fans_missing"
KINDS = (KIND_DRIVE_HOT, KIND_FAN_STALLED, KIND_SMART_FAILING, KIND_SMART_WARNING, KIND_SAFETY, KIND_FANS_MISSING)

# Params that change at every sample: they are not part of what was taken note of.
VOLATILE_PARAMS = ("temp", "percent")
# Warnings of the web UI itself, not alerts, that can be taken note of too.
BANNER_KEYS = ("banner:no_auth",)
ACK_FILE = "alerts.json"

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
#   Alerts
# ---------------------------
class Alerts:
    """The alerts in force, and what to do when one starts or ends."""

    def __init__(
        self,
        settings: Callable[[], Settings],
        log: Callable[[str], None],
        sender: Sender = send,
        config_dir: Path | None = None,
    ) -> None:
        """Read the address and language from settings(), log with log, send with sender.

        With config_dir, the notes taken survive a restart in alerts.json.
        """
        self._settings = settings
        self._log = log
        self._send = sender
        self._ack_path = None if config_dir is None else config_dir / ACK_FILE
        self.active: dict[str, dict[str, Any]] = {}
        # key -> signature of the alert when it was taken note of.
        self.acks: dict[str, str] = {}
        if self._ack_path is not None:
            try:
                data = json.loads(self._ack_path.read_text(encoding="utf-8"))
                self.acks = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.acks = {}

    @staticmethod
    def signature(params: dict[str, Any]) -> str:
        """What an alert is about, without the values that change at every sample."""
        return json.dumps({k: v for k, v in params.items() if k not in VOLATILE_PARAMS}, sort_keys=True)

    def _save_acks(self) -> None:
        """Keep the notes taken. Blocking."""
        if self._ack_path is not None:
            write_atomic(self._ack_path, json.dumps(self.acks, indent=2))

    def ack(self, key: str) -> bool:
        """Take note of an active alert, or of a warning of the web UI. Blocking. False if unknown."""
        if key in self.active:
            self.acks[key] = self.signature(self.active[key]["params"])
        elif key in BANNER_KEYS:
            self.acks[key] = ""
        else:
            return False
        self._save_acks()
        return True

    def unack(self, key: str) -> None:
        """Show again what was taken note of. Blocking."""
        if self.acks.pop(key, None) is not None:
            self._save_acks()

    async def update(self, conditions: dict[str, Condition]) -> None:
        """Raise the alerts that start now and end those that are over."""
        for key, (kind, params) in conditions.items():
            if key in self.active:
                self.active[key]["params"] = params
                continue
            self.active[key] = {"kind": kind, "params": params, "since": time.time()}
            self._log("ALERT: " + render("en", f"alert.{kind}", params))
            await self._notify(f"alert.{kind}", params, urgent=True)
        # A note lasts while the alert does: the next time it starts, it shows again.
        dropped = [k for k in self.acks if k not in BANNER_KEYS and k not in conditions]
        for key in dropped:
            del self.acks[key]
        if dropped:
            await asyncio.to_thread(self._save_acks)
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
        return [
            {"key": key, **alert, "acknowledged": self.acks.get(key) == self.signature(alert["params"])}
            for key, alert in sorted(self.active.items(), key=lambda item: item[1]["since"])
        ]
