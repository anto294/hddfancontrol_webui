"""The SQLite history and the SMART store."""

from __future__ import annotations

import sqlite3
import time
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


def test_failed_commit_does_not_block_the_next(tmp_path: Path) -> None:
    """A COMMIT that fails (SQLITE_BUSY leaves the transaction open, as SQLite documents) is rolled back too."""
    history = History(tmp_path / "h.db")
    real = history._db

    class BusyCommit:
        """The connection, but COMMIT fails and leaves the transaction open."""

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def execute(self, sql: str, *args: Any) -> Any:
            if sql == "COMMIT":
                raise sqlite3.OperationalError("database is locked")
            return real.execute(sql, *args)

    history._db = BusyCommit()  # type: ignore[assignment]  # a stand-in for the connection
    with pytest.raises(sqlite3.OperationalError):
        history.record(NOW, [("temp", "sda", 40.0)])
    history._db = real
    assert not real.in_transaction
    history.record(NOW, [("temp", "sda", 41.0)])  # the failed sample was rolled back: this one is stored
    assert history.query(1, now=NOW)["series"][0]["points"] == [[NOW, 41.0]]
    history.close()


def test_history_cleans_itself(tmp_path: Path) -> None:
    """Empty series go with the purge; SMART data of drives not kept is forgotten."""
    history = History(tmp_path / "history.db")
    history.record(1000, [("temp", "/dev/disk/by-id/gone", 40.0)])
    history.record(int(time.time()), [("temp", "/dev/disk/by-id/here", 41.0)])
    history.purge(1)
    assert [s["name"] for s in history.query(24 * 365 * 60)["series"]] == ["/dev/disk/by-id/here"]
    history.record(int(time.time()), [("temp", "/dev/disk/by-id/gone", 39.0)])  # it can come back
    assert len(history.query(1)["series"]) == 2
    history.save_smart("/dev/a", {"status": "ok"})
    history.save_smart("/dev/b", {"status": "ok"})
    assert history.forget_smart({"/dev/a"}) == 1 and list(history.smart()) == ["/dev/a"]
    history.save_note("k", "s")
    history.delete_notes(["k", "other"])
    assert history.notes() == {}
    history.close()


def test_a_corrupt_database_is_set_aside(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A history.db that is not a database is renamed history.db.corrupt-<time>, and a new one is made."""
    path = tmp_path / "history.db"
    path.write_bytes(b"this is not a database, " * 200)
    (tmp_path / "history.db-wal").write_bytes(b"junk")
    history = History(path)
    history.record(1000, [("temp", "a", 40.0)])
    assert history.query(1, now=1000)
    history.close()
    aside = sorted(p.name for p in tmp_path.iterdir() if ".corrupt-" in p.name)
    assert aside and aside[0].startswith("history.db.corrupt-")  # SQLite may already have dropped the -wal junk
    assert any("corrupt" in record.message for record in caplog.records)
