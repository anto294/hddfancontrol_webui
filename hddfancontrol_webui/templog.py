"""Read the newest sample of the temperature log that hddfancontrol writes.

hddfancontrol runs with ``--temp-log`` and appends one line per check:

    {"time_utc": "2026-02-13T12:50:01Z",
     "measures": [{"device": "sda ST4000VN006", "temp_celcius": 41.0},
                  {"device": "sdb WD40EFRX", "temp_celcius": null}]}

A null temperature means the drive was asleep and hddfancontrol did not
wake it. Reading this log, instead of probing the drives, keeps the web UI
from waking drives or sending them a second stream of SMART commands.

Only the end of the file is read, and only when it changed, so this costs
the same however long the file gets. The history lives in the database
(history.py), not here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# One sample is a few hundred bytes per drive; this holds a whole line for
# dozens of drives.
TAIL_BYTES = 16384


# ---------------------------
#   parse_line
# ---------------------------
def parse_line(line: str) -> dict[str, Any] | None:
    """Parse one line of the log into {time, temps}, or None if it is not a sample.

    A temperature that is no finite number (NaN, an infinity) makes the
    whole line no sample: NaN would pass every comparison with a maximum,
    and dropping only that drive would let its fans slow down. With no
    sample, fan control runs into its stale failsafe instead.
    """
    try:
        data = json.loads(line)
        time = datetime.fromisoformat(data["time_utc"].replace("Z", "+00:00"))
        temps: dict[str, float | None] = {}
        for measure in data["measures"]:
            value = measure["temp_celcius"]
            temp = None if value is None else float(value)
            if temp is not None and not math.isfinite(temp):
                return None
            temps[str(measure["device"])] = temp
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if time.tzinfo is None:
        time = time.replace(tzinfo=UTC)
    return {"time": time.isoformat(), "temps": temps}


# ---------------------------
#   TempLog
# ---------------------------
class TempLog:
    """Follow the newest sample of the log."""

    def __init__(self, path: Path) -> None:
        """Follow this file."""
        self.path = path
        self._key: tuple[int, int] | None = None
        self._latest: dict[str, Any] | None = None
        # Samples up to this time belong to an earlier run of hddfancontrol (fence).
        self._fence: datetime | None = None
        # The web server and the fan controller read from different threads.
        self._lock = threading.Lock()

    def latest(self) -> dict[str, Any] | None:
        """Return the newest sample, or None before the first one of this run of hddfancontrol.

        After the daily rotation the file is empty for one interval: the
        previous sample stays, with its own time, so nothing mistakes the
        rotation for a stopped hddfancontrol. A sample from before the
        last fence() is no sample.
        """
        with self._lock:
            sample = self._newest()
            if sample is not None and self._fence is not None and datetime.fromisoformat(sample["time"]) <= self._fence:
                return None
            return sample

    def fence(self) -> None:
        """hddfancontrol starts again: the samples written so far are not read any more.

        Which values of a sample are drives and which are sensors depends
        on the settings it was written with, so a sample of the run before
        must never be read with new ones; it would also count as fresh.
        """
        with self._lock:
            sample = self._newest()
            if sample is not None:
                self._fence = datetime.fromisoformat(sample["time"])

    def _newest(self) -> dict[str, Any] | None:
        """The newest sample of the file, read again only when it changed. The lock is held."""
        try:
            info = self.path.stat()
        except OSError:
            return self._latest
        key = (info.st_mtime_ns, info.st_size)
        if key == self._key or info.st_size == 0:
            return self._latest
        self._key = key
        sample = self._read_last(info.st_size)
        if sample is not None:
            self._latest = sample
        return self._latest

    def _read_last(self, size: int) -> dict[str, Any] | None:
        """Read the last complete line of the file."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(max(0, size - TAIL_BYTES), os.SEEK_SET)
                tail = handle.read(TAIL_BYTES)
        except OSError as err:
            _LOGGER.warning("Cannot read %s: %s", self.path, err)
            return None
        # The last piece may be a line that hddfancontrol is still writing.
        for raw in reversed(tail.split(b"\n")):
            sample = parse_line(raw.decode("utf-8", errors="replace")) if raw.strip() else None
            if sample is not None:
                return sample
        return None
