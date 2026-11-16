"""The newest sample of the hddfancontrol temperature log."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hddfancontrol_webui.templog import TAIL_BYTES, TempLog, parse_line


def line(time: str, *temps: tuple[str, float | None]) -> str:
    """Build a log line as hddfancontrol writes it."""
    return json.dumps({"time_utc": time, "measures": [{"device": d, "temp_celcius": t} for d, t in temps]}) + "\n"


def test_parse_line() -> None:
    """A sample gives its time and temperatures; anything else gives None."""
    sample = parse_line(line("2026-02-13T12:50:01Z", ("sda X", 41.0), ("sdb Y", None)))
    assert sample == {"time": "2026-02-13T12:50:01+00:00", "temps": {"sda X": 41.0, "sdb Y": None}}
    assert parse_line("not json") is None
    assert parse_line('{"time_utc": "2026-02-13T12:50:01"}') is None
    naive = parse_line(line("2026-02-13T12:50:01", ("a", 1)))
    assert naive is not None and naive["time"].endswith("+00:00")


def test_latest_follows_the_file(tmp_path: Path) -> None:
    """The last complete line wins; a half-written line is skipped."""
    path = tmp_path / "temps.jsonl"
    log = TempLog(path)
    assert log.latest() is None

    path.write_text(line("2026-02-13T12:00:00Z", ("sda", 40.0)) + line("2026-02-13T12:00:20Z", ("sda", 41.0)))
    latest = log.latest()
    assert latest is not None and latest["temps"] == {"sda": 41.0}

    with path.open("a") as handle:
        handle.write('{"time_utc": "2026-02-13T12:00:4')
    os.utime(path, ns=(1, 2))
    latest = log.latest()
    assert latest is not None and latest["temps"] == {"sda": 41.0}


def test_rotation_keeps_the_last_sample(tmp_path: Path) -> None:
    """After the daily rotation the file is empty: the previous sample stays."""
    path = tmp_path / "temps.jsonl"
    path.write_text(line("2026-02-13T23:59:50Z", ("sda", 39.0)))
    log = TempLog(path)
    assert log.latest() is not None
    path.write_text("")
    latest = log.latest()
    assert latest is not None and latest["temps"] == {"sda": 39.0}
    path.unlink()
    assert log.latest() is not None


def test_only_the_tail_is_read(tmp_path: Path) -> None:
    """A long file is read from its end only."""
    path = tmp_path / "temps.jsonl"
    old = line("2026-02-13T00:00:00Z", ("sda", 30.0))
    path.write_text(old * (3 * TAIL_BYTES // len(old)) + line("2026-02-13T23:00:00Z", ("sda", 45.0)))
    latest = TempLog(path).latest()
    assert latest is not None and latest["temps"] == {"sda": 45.0}


def test_garbage_only(tmp_path: Path) -> None:
    """A file without any sample gives None."""
    path = tmp_path / "temps.jsonl"
    path.write_text("garbage\n\n")
    assert TempLog(path).latest() is None
