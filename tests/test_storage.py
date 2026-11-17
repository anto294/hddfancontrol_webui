"""Atomic writes of the files of /config."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hddfancontrol_webui.storage import write_atomic


def test_a_planted_temporary_name_is_never_followed(tmp_path: Path) -> None:
    """A link left at the old fixed temporary name (settings.json.tmp) is neither written through nor in the way."""
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / "settings.json.tmp").symlink_to(victim)
    write_atomic(tmp_path / "settings.json", "new")
    assert victim.read_text() == "untouched" and (tmp_path / "settings.json").read_text() == "new"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".settings.json")] == []  # no temporary left


def test_a_failed_write_leaves_no_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the rename fails, the temporary file is removed and the old file stays."""
    target = tmp_path / "labels.json"
    target.write_text("old")

    def broken(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        write_atomic(target, "new")
    assert target.read_text() == "old" and sorted(p.name for p in tmp_path.iterdir()) == ["labels.json"]
