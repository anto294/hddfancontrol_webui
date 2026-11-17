"""The bay map: checks, storage and API."""

from __future__ import annotations

from pathlib import Path

import pytest

from hddfancontrol_webui.bays import BayError, Bays, check_bays

A = "/dev/disk/by-id/ata-A"
B = "/dev/disk/by-id/ata-B"


def test_check_bays() -> None:
    """A grid of rows × cols slots, each a drive path or empty, no drive twice."""
    assert check_bays({"rows": 1, "cols": 2, "slots": [A, ""]}) == {"rows": 1, "cols": 2, "slots": [A, ""]}
    assert check_bays({"rows": 0, "cols": 0}) == {"rows": 0, "cols": 0, "slots": []}
    for bad, code in (
        ([], "invalid_document"),
        ({"rows": 13, "cols": 1, "slots": [""] * 13}, "out_of_range"),
        ({"rows": True, "cols": 1, "slots": [""]}, "out_of_range"),
        ({"rows": 1, "cols": 2, "slots": [A]}, "invalid_document"),
        ({"rows": 1, "cols": 2, "slots": [A, "/etc/passwd"]}, "invalid_dev_path"),
        ({"rows": 1, "cols": 2, "slots": [A, A]}, "duplicate"),
    ):
        with pytest.raises(BayError) as err:
            check_bays(bad)
        assert err.value.code == code


def test_store(tmp_path: Path) -> None:
    """The map is written to bays.json and read back; a broken file starts empty."""
    bays = Bays(tmp_path)
    assert bays.get() == {"rows": 0, "cols": 0, "slots": []}
    bays.set({"rows": 2, "cols": 1, "slots": [A, B]})
    assert Bays(tmp_path).get()["slots"] == [A, B]
    (tmp_path / "bays.json").write_text("{nope")
    assert Bays(tmp_path).get()["rows"] == 0


@pytest.mark.parametrize("content", [b"[" * 100000, b'{"rows": "\xe9"}'])
def test_a_broken_file_starts_without_a_map(tmp_path: Path, content: bytes) -> None:
    """bays.json nested too deep or not UTF-8: no map, never a failed start."""
    (tmp_path / "bays.json").write_bytes(content)
    assert Bays(tmp_path).get()["slots"] == []
