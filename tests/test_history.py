"""The SQLite history and the SMART store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui.const import HISTORY_MAX_POINTS
from hddfancontrol_webui.history import History

NOW = 1_800_000_000


def test_record_and_query(tmp_path: Path) -> None:
    """Samples come back per series, averaged into buckets, never more than the maximum."""
    history = History(tmp_path / "db" / "history.db")
    for i in range(3600):
        ts = NOW - 3600 + i
        history.record(ts, [("temp", "sda", 40 + (i % 2)), ("rpm", "/sys/p1", 800)])
    history.record(NOW - 10, [("temp", "sda", 99)])  # repeated time: ignored
    data = history.query(1, now=NOW)
    assert data["bucket"] == 6
    series = {(s["kind"], s["name"]): s["points"] for s in data["series"]}
    assert set(series) == {("temp", "sda"), ("rpm", "/sys/p1")}
    assert len(series[("temp", "sda")]) <= HISTORY_MAX_POINTS + 1
    assert all(p[1] == 40.5 for p in series[("temp", "sda")])
    history.close()


def test_series_survive_reopening(tmp_path: Path) -> None:
    """Series ids are read back, and purge removes old samples."""
    path = tmp_path / "history.db"
    history = History(path)
    history.record(NOW - 40 * 86400, [("temp", "sda", 30)])
    history.record(NOW, [("temp", "sda", 31)])
    history.close()
    history = History(path)
    history.record(NOW + 1, [("temp", "sda", 32)])
    assert history.purge(30, now=NOW) == 1
    data = history.query(24 * 60, now=NOW + 1)
    values = [p[1] for p in data["series"][0]["points"]]
    assert values and min(values) >= 31
    history.close()


def test_smart_store(tmp_path: Path) -> None:
    """The last SMART data of each drive is kept with its time."""
    history = History(tmp_path / "history.db")
    history.save_smart("/dev/a", {"status": "ok"}, ts=NOW)
    history.save_smart("/dev/a", {"status": "warning"}, ts=NOW + 5)
    assert history.smart() == {"/dev/a": {"time": NOW + 5, "status": "warning"}}
    history.close()


def test_failed_record_does_not_block_the_next(tmp_path: Path) -> None:
    """A failed insert (a full disk, say) is rolled back, so later samples are still stored."""
    history = History(tmp_path / "h.db")
    real = history._db

    class FullDisk:
        """The connection, but the next executemany fails."""

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def executemany(self, *args: Any) -> Any:
            raise sqlite3.OperationalError("database or disk is full")

    history._db = FullDisk()  # type: ignore[assignment]  # a stand-in for the connection
    with pytest.raises(sqlite3.OperationalError):
        history.record(NOW, [("temp", "sda", 40.0)])
    history._db = real
    history.record(NOW, [("temp", "sda", 40.0)])
    assert history.query(1, now=NOW)["series"][0]["points"] == [[NOW, 40.0]]
    history.close()
