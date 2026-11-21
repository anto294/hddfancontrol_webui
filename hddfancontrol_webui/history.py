"""History of temperatures and fan speeds, and the last SMART data, in SQLite.

The database is /config/history.db. A series is one thing measured over time:
the temperature of a drive or a hwmon input, or the PWM (in %) or the speed
(in RPM) of a fan. Every sample is a time in seconds and a value. It also
keeps the notes taken on alerts (alerts.py).

It cleans itself: purge() deletes old samples and the series left without
any, and forget_smart() the SMART data of drives no longer monitored (an
archived drive keeps its own: the caller keeps it). forget_drive() drops
everything of one drive, when the user deletes a drive that is gone.

Long windows are answered by averaging the samples of equal time buckets in
SQL, so a month never sends more than HISTORY_MAX_POINTS points per series.

Every method blocks, and the web server calls them from worker threads.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .const import HISTORY_MAX_POINTS

_LOGGER = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS alert_notes (
    key TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archived (
    device TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
"""

# The alerts and the meta keys that belong to one monitored drive (alerts.py, monitor.py).
DRIVE_ALERT_KEYS = ("missing:{}", "smart:{}", "smart_error:{}")
DRIVE_META_KEYS = ("selftest_short_{}", "selftest_long_{}", "selftest_long_started_{}", "drive_ids:{}")


# ---------------------------
#   History
# ---------------------------
class History:
    """Store and read the samples."""

    def __init__(self, path: Path) -> None:
        """Open, or create, the database; a corrupt one is set aside and a new one made.

        The history is not worth a core that does not start: a file that
        SQLite cannot open (a power cut on a bad disk, a stray copy) is
        renamed history.db.corrupt-<time>, with its -wal and -shm files,
        and the error is logged.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._series: dict[tuple[str, str], int] = {}
        try:
            self._db = self._open(path)
        except sqlite3.DatabaseError as err:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                part = path.with_name(path.name + suffix)
                if part.exists():
                    part.rename(path.with_name(f"{path.name}.corrupt-{stamp}{suffix}"))
            _LOGGER.error(
                "%s is corrupt (%s): set aside as %s.corrupt-%s, starting a new history", path, err, path.name, stamp
            )
            self._series.clear()
            self._db = self._open(path)

    def _open(self, path: Path) -> sqlite3.Connection:
        """Connect to the database, set it up and read the series. Raises sqlite3.DatabaseError."""
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            with self._lock:
                # WAL lets a read run while the recorder writes; NORMAL is safe with WAL.
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                db.executescript(_SCHEMA)
                for series_id, kind, name in db.execute("SELECT id, kind, name FROM series"):
                    self._series[(kind, name)] = series_id
        except sqlite3.DatabaseError:
            db.close()
            raise
        return db

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
            try:
                self._db.executemany("INSERT OR IGNORE INTO samples (series, ts, value) VALUES (?, ?, ?)", rows)
                # A COMMIT that fails with SQLITE_BUSY leaves the transaction open, as SQLite documents.
                self._db.execute("COMMIT")
            except sqlite3.Error:
                # Left open, the transaction would make every later BEGIN fail. SQLite may
                # have rolled it back itself already (a full disk): then there is nothing to undo.
                if self._db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        self._db.execute("ROLLBACK")
                raise

    def purge(self, days: int, now: float | None = None) -> int:
        """Delete the samples older than days, then the series left empty; return how many samples."""
        limit = int((now or time.time()) - days * 86400)
        with self._lock:
            deleted = self._db.execute("DELETE FROM samples WHERE ts < ?", (limit,)).rowcount
            # A drive or fan gone for longer than the history leaves an empty series behind.
            empty = self._db.execute(
                "SELECT id, kind, name FROM series WHERE NOT EXISTS (SELECT 1 FROM samples WHERE series = series.id)"
            ).fetchall()
            for series_id, kind, name in empty:
                self._db.execute("DELETE FROM series WHERE id = ?", (series_id,))
                self._series.pop((kind, name), None)
            return deleted

    def query(self, hours: float, now: float | None = None, max_points: int = HISTORY_MAX_POINTS) -> dict[str, Any]:
        """Return every series of the last hours, averaged into at most max_points buckets."""
        end = int(now or time.time())
        start = end - int(hours * 3600)
        bucket = max(1, -(-(end - start) // max_points))
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

    def forget_smart(self, keep: set[str]) -> int:
        """Delete the SMART data of every drive not in keep (no longer monitored or gone); return how many."""
        with self._lock:
            devices = [row[0] for row in self._db.execute("SELECT device FROM smart")]
            gone = [(device,) for device in devices if device not in keep]
            self._db.executemany("DELETE FROM smart WHERE device = ?", gone)
            return len(gone)

    def notes(self) -> dict[str, str]:
        """Return the notes taken on alerts, as key -> signature."""
        with self._lock:
            return dict(self._db.execute("SELECT key, signature FROM alert_notes").fetchall())

    def save_note(self, key: str, signature: str) -> None:
        """Keep a note on an alert."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO alert_notes (key, signature, ts) VALUES (?, ?, ?)", (key, signature, int(time.time()))
            )

    def delete_notes(self, keys: list[str]) -> None:
        """Forget the notes on these alerts."""
        with self._lock:
            self._db.executemany("DELETE FROM alert_notes WHERE key = ?", [(key,) for key in keys])

    def stats(self, since: int) -> list[tuple[str, str, float, float, float]]:
        """(kind, name, minimum, average, maximum) of every series since a time."""
        with self._lock:
            rows = self._db.execute(
                "SELECT s.kind, s.name, MIN(m.value), AVG(m.value), MAX(m.value) "
                "FROM samples m JOIN series s ON s.id = m.series WHERE m.ts >= ? GROUP BY m.series ORDER BY s.kind, s.name",
                (since,),
            ).fetchall()
        return [(kind, name, low, avg, high) for kind, name, low, avg, high in rows]

    def get_meta(self, key: str) -> str | None:
        """A small value kept by the web UI (when the last summary went out, for example)."""
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        """Keep a small value."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def oldest(self, kind: str | None = None, name: str | None = None) -> int | None:
        """The time of the oldest sample, of every series or of one (kind, name), or None."""
        with self._lock:
            if kind is None or name is None:
                row = self._db.execute("SELECT MIN(ts) FROM samples").fetchone()
            else:
                row = self._db.execute(
                    "SELECT MIN(s.ts) FROM samples s JOIN series r ON r.id = s.series WHERE r.kind = ? AND r.name = ?",
                    (kind, name),
                ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def fan_curve(self, path: str, start: int, end: int, step: float = 5.0) -> dict[float, tuple[float, int]]:
        """Average RPM of a fan for each PWM band (step %), between two times: {band: (rpm, samples)}.

        PWM and RPM are recorded together, so a sample of each with the same
        time make a pair; a fan standing still (0 RPM) does not count.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT ROUND(p.value / ?) * ? AS band, AVG(r.value), COUNT(*) "
                "FROM samples p "
                "JOIN series sp ON sp.id = p.series AND sp.kind = 'pwm' AND sp.name = ? "
                "JOIN series sr ON sr.kind = 'rpm' AND sr.name = ? "
                "JOIN samples r ON r.series = sr.id AND r.ts = p.ts "
                "WHERE p.ts >= ? AND p.ts <= ? AND r.value > 0 GROUP BY band",
                (step, step, path, path, start, end),
            ).fetchall()
        return {float(band): (float(rpm), int(count)) for band, rpm, count in rows}

    def remember_ids(self, device: str, ids: list[str]) -> None:
        """Keep the other names of a monitored drive (its by-id links), to find its series once it is gone."""
        self.set_meta(f"drive_ids:{device}", json.dumps(sorted(ids)))

    def drive_ids(self, device: str) -> list[str]:
        """The names of a drive kept by remember_ids, or []."""
        saved = self.get_meta(f"drive_ids:{device}")
        return [] if saved is None else [str(name) for name in json.loads(saved)]

    def archive(self, device: str, ts: int | None = None) -> None:
        """Mark a drive as archived: no longer monitored, its last SMART data and its series kept."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO archived (device, ts) VALUES (?, ?)", (device, int(ts or time.time())))

    def unarchive(self, device: str) -> None:
        """A drive monitored again is no longer archived."""
        with self._lock:
            self._db.execute("DELETE FROM archived WHERE device = ?", (device,))

    def archived(self) -> dict[str, int]:
        """The archived drives, with when they were archived."""
        with self._lock:
            return dict(self._db.execute("SELECT device, ts FROM archived").fetchall())

    def forget_drive(self, device: str, names: set[str]) -> None:
        """Delete everything kept of a drive: SMART data, archive mark, the series named in names, notes and meta keys."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM smart WHERE device = ?", (device,))
                self._db.execute("DELETE FROM archived WHERE device = ?", (device,))
                for pattern in DRIVE_ALERT_KEYS:
                    self._db.execute("DELETE FROM alert_notes WHERE key = ?", (pattern.format(device),))
                for pattern in DRIVE_META_KEYS:
                    self._db.execute("DELETE FROM meta WHERE key = ?", (pattern.format(device),))
                gone = [(key, series_id) for key, series_id in self._series.items() if key[1] in names]
                for _key, series_id in gone:
                    self._db.execute("DELETE FROM samples WHERE series = ?", (series_id,))
                    self._db.execute("DELETE FROM series WHERE id = ?", (series_id,))
                self._db.execute("COMMIT")
            except sqlite3.Error:
                if self._db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        self._db.execute("ROLLBACK")
                raise
            for key, _series_id in gone:
                self._series.pop(key, None)
