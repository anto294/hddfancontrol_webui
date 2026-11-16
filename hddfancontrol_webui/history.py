"""History of temperatures and fan speeds, and the last SMART data, in SQLite.

The database is /config/history.db. A series is one thing measured over time:
the temperature of a drive or a hwmon input, or the PWM (in %) or the speed
(in RPM) of a fan. Every sample is a time in seconds and a value.

Long windows are answered by averaging the samples of equal time buckets in
SQL, so a month never sends more than HISTORY_MAX_POINTS points per series.

Every method blocks, and the web server calls them from worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .const import HISTORY_MAX_POINTS

KINDS = ("temp", "pwm", "rpm")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (kind, name)
);
CREATE TABLE IF NOT EXISTS samples (
    series INTEGER NOT NULL REFERENCES series (id),
    ts INTEGER NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (series, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS smart (
    device TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    data TEXT NOT NULL
);
"""


# ---------------------------
#   History
# ---------------------------
class History:
    """Store and read the samples."""

    def __init__(self, path: Path) -> None:
        """Open, or create, the database."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._series: dict[tuple[str, str], int] = {}
        with self._lock:
            # WAL lets a read run while the recorder writes; NORMAL is safe with WAL.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            for series_id, kind, name in self._db.execute("SELECT id, kind, name FROM series"):
                self._series[(kind, name)] = series_id

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            self._db.close()

    def _series_id(self, kind: str, name: str) -> int:
        """Return the id of a series, creating it the first time. The lock is held."""
        key = (kind, name)
        if key not in self._series:
            cursor = self._db.execute("INSERT INTO series (kind, name) VALUES (?, ?)", key)
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError(f"No id for the series {key}")
            self._series[key] = cursor.lastrowid
        return self._series[key]

    def record(self, ts: int, values: list[tuple[str, str, float]]) -> None:
        """Store (kind, name, value) samples taken at ts. A repeated sample is ignored."""
        with self._lock:
            rows = [(self._series_id(kind, name), ts, value) for kind, name, value in values]
            self._db.execute("BEGIN")
            self._db.executemany("INSERT OR IGNORE INTO samples (series, ts, value) VALUES (?, ?, ?)", rows)
            self._db.execute("COMMIT")

    def purge(self, days: int, now: float | None = None) -> int:
        """Delete the samples older than days, and return how many."""
        limit = int((now or time.time()) - days * 86400)
        with self._lock:
            return self._db.execute("DELETE FROM samples WHERE ts < ?", (limit,)).rowcount

    def query(self, hours: float, now: float | None = None) -> dict[str, Any]:
        """Return every series of the last hours, averaged into at most HISTORY_MAX_POINTS buckets."""
        end = int(now or time.time())
        start = end - int(hours * 3600)
        bucket = max(1, -(-(end - start) // HISTORY_MAX_POINTS))
        with self._lock:
            rows = self._db.execute(
                "SELECT s.kind, s.name, (m.ts / ?) * ? AS b, AVG(m.value) "
                "FROM samples m JOIN series s ON s.id = m.series "
                "WHERE m.ts >= ? AND m.ts <= ? GROUP BY m.series, b ORDER BY s.kind, s.name, b",
                (bucket, bucket, start, end),
            ).fetchall()
        series: dict[tuple[str, str], list[list[float]]] = {}
        for kind, name, ts, value in rows:
            series.setdefault((kind, name), []).append([ts, round(value, 2)])
        return {
            "start": start,
            "end": end,
            "bucket": bucket,
            "series": [{"kind": k, "name": n, "points": p} for (k, n), p in series.items()],
        }

    def save_smart(self, device: str, data: dict[str, Any], ts: int | None = None) -> None:
        """Keep the last SMART data of a drive."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO smart (device, ts, data) VALUES (?, ?, ?)",
                (device, int(ts or time.time()), json.dumps(data)),
            )

    def smart(self) -> dict[str, dict[str, Any]]:
        """Return the last SMART data of every drive, with its time."""
        with self._lock:
            rows = self._db.execute("SELECT device, ts, data FROM smart").fetchall()
        return {device: {"time": ts, **json.loads(data)} for device, ts, data in rows}
